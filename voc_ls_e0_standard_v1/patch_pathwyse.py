#!/usr/bin/env python3
"""Apply a narrowly scoped, reversible E0 diagnostic probe to PathWyse v1.0.

No Git or network access. Verifies exact source anchors before changing anything.
"""
from __future__ import annotations
import argparse
import difflib
import pathlib
import shutil

LM_H = 'src/core/algorithms/dynamic_programming/PW_default/LM_default.h'
LM_CPP = 'src/core/algorithms/dynamic_programming/PW_default/LM_default.cpp'
PW_CPP = 'src/core/algorithms/dynamic_programming/PW_default/PW_default.cpp'
MARK = 'VOC_E0_PROFILE_PATCH_V1'

def once(text, before, after, name):
    n=text.count(before)
    if n!=1: raise ValueError(f'{name}: expected one anchor, got {n}: {before!r}')
    return text.replace(before,after,1)

def patch_h(t):
    t=once(t, '#include <set>\n', '#include <set>\n#include <atomic>\n#include <cstdint>\n', LM_H)
    t=once(t, '    bool dominates(LabelAdv *l1, LabelAdv *l2);', '''    bool dominates(LabelAdv *l1, LabelAdv *l2);
    // VOC_E0_PROFILE_PATCH_V1: profiling-only; no learned scorer in B0.
    void resetE0Profile();
    void recordE0InferenceNs(std::uint64_t elapsed_ns);''',LM_H)
    t=once(t,'protected:\n\n    std::string name;', '''protected:
    bool dominatesCore(LabelAdv *l1, LabelAdv *l2);
    bool e0_profile_enabled = false;
    std::atomic<std::uint64_t> e0_dom_fw{0}, e0_dom_bw{0};
    std::atomic<std::uint64_t> e0_dom_time_fw_ns{0}, e0_dom_time_bw_ns{0};
    std::atomic<std::uint64_t> e0_inference_calls{0}, e0_inference_time_ns{0};

    std::string name;''',LM_H)
    return t

def patch_lm_cpp(t):
    t=once(t,'#include <algorithm>\n','#include <algorithm>\n#include <chrono>\n#include <cstdlib>\n',LM_CPP)
    t=once(t,'LMDefault::LMDefault(Problem* problem) {','''LMDefault::LMDefault(Problem* problem) {
    // VOC_E0_PROFILE_PATCH_V1: enable only for non-benchmark diagnostic passes.
    const char* e0_flag = std::getenv("VOC_E0_PROFILE");
    e0_profile_enabled = e0_flag != nullptr && std::string(e0_flag) == "1";''', LM_CPP)
    original='//Returns true if l1 dominates l2\nbool LMDefault::dominates(LabelAdv* l1, LabelAdv* l2)  {'
    wrapped='''// VOC_E0_PROFILE_PATCH_V1: count actual dominance predicate calls.
// This wrapper is off during benchmark timing; measured time is diagnostic-only
// and includes the timer instrumentation itself (not an unbiased solver cost).
void LMDefault::resetE0Profile() {
    e0_dom_fw.store(0, std::memory_order_relaxed);
    e0_dom_bw.store(0, std::memory_order_relaxed);
    e0_dom_time_fw_ns.store(0, std::memory_order_relaxed);
    e0_dom_time_bw_ns.store(0, std::memory_order_relaxed);
    e0_inference_calls.store(0, std::memory_order_relaxed);
    e0_inference_time_ns.store(0, std::memory_order_relaxed);
}

void LMDefault::recordE0InferenceNs(std::uint64_t elapsed_ns) {
    // For future E1 call this after each actual model inference.
    if (!e0_profile_enabled) return;
    e0_inference_calls.fetch_add(1, std::memory_order_relaxed);
    e0_inference_time_ns.fetch_add(elapsed_ns, std::memory_order_relaxed);
}

bool LMDefault::dominates(LabelAdv* l1, LabelAdv* l2) {
    if (!e0_profile_enabled) return dominatesCore(l1, l2);
    const bool fw = l1->getDirection();
    auto &count = fw ? e0_dom_fw : e0_dom_bw;
    auto &duration = fw ? e0_dom_time_fw_ns : e0_dom_time_bw_ns;
    count.fetch_add(1, std::memory_order_relaxed);
    const auto begin = std::chrono::steady_clock::now();
    const bool answer = dominatesCore(l1, l2);
    const auto elapsed_ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now() - begin).count();
    duration.fetch_add(static_cast<std::uint64_t>(elapsed_ns), std::memory_order_relaxed);
    return answer;
}

//Returns true if l1 dominates l2
bool LMDefault::dominatesCore(LabelAdv* l1, LabelAdv* l2)  {'''
    t=once(t, original,wrapped,LM_CPP)
    t=once(t,'    collector.init("ndominated", 0);','''    collector.init("ndominated", 0);
    // E0 diagnostics; double counters remain exact below 2**53.
    collector.init("e0_dom_checks_fw", 0.0);
    collector.init("e0_dom_checks_bw", 0.0);
    collector.init("e0_dom_time_fw_ns", 0.0);
    collector.init("e0_dom_time_bw_ns", 0.0);
    collector.init("e0_inference_calls", 0.0);
    collector.init("e0_inference_time_ns", 0.0);''', LM_CPP)
    t=once(t,'    collector.collect("ndominated", ndominated_fw + ndominated_bw);','''    collector.collect("ndominated", ndominated_fw + ndominated_bw);
    collector.collect("e0_dom_checks_fw", static_cast<double>(e0_dom_fw.load(std::memory_order_relaxed)));
    collector.collect("e0_dom_checks_bw", static_cast<double>(e0_dom_bw.load(std::memory_order_relaxed)));
    collector.collect("e0_dom_time_fw_ns", static_cast<double>(e0_dom_time_fw_ns.load(std::memory_order_relaxed)));
    collector.collect("e0_dom_time_bw_ns", static_cast<double>(e0_dom_time_bw_ns.load(std::memory_order_relaxed)));
    collector.collect("e0_inference_calls", static_cast<double>(e0_inference_calls.load(std::memory_order_relaxed)));
    collector.collect("e0_inference_time_ns", static_cast<double>(e0_inference_time_ns.load(std::memory_order_relaxed)));''',LM_CPP)
    return t

def patch_pw(t):
    t=once(t,'    label_manager->setIteration(iterations);\n    label_manager->initLM();', '''    label_manager->setIteration(iterations);
    label_manager->initLM();
    label_manager->resetE0Profile(); // VOC_E0_PROFILE_PATCH_V1, per-DSSR-round reset''',PW_CPP)
    return t

def main():
    p=argparse.ArgumentParser();p.add_argument('--repo',required=True,type=pathlib.Path);p.add_argument('--dry-run',action='store_true');p.add_argument('--restore',action='store_true');args=p.parse_args()
    root=args.repo.expanduser().resolve()
    files={LM_H:patch_h,LM_CPP:patch_lm_cpp,PW_CPP:patch_pw}
    changes=[]
    for name,patch in files.items():
        path=root/name
        if not path.is_file():raise SystemExit(f'Missing PathWyse source: {path}')
        original=path.read_text(encoding='utf-8')
        backup=path.with_suffix(path.suffix+'.e0_original')
        if args.restore:
            if not backup.is_file():raise SystemExit(f'Missing backup: {backup}')
            changed=backup.read_text(encoding='utf-8')
        elif MARK in original:
            print(f'Already patched: {name}');continue
        else:changed=patch(original)
        changes.append((path,original,changed,backup))
    if not changes:print('No changes required');return
    if args.dry_run:
        for path,old,new,_ in changes:
            diff=list(difflib.unified_diff(old.splitlines(),new.splitlines(),fromfile=str(path),tofile=str(path)+' (E0 patched)',lineterm=''))
            print('\n'.join(diff[:110]))
        print(f'DRY RUN: {len(changes)} files would change.');return
    for path,old,new,backup in changes:
        if not args.restore and not backup.exists():shutil.copy2(path,backup)
        path.write_text(new,encoding='utf-8')
        print(('RESTORED' if args.restore else 'PATCHED')+': '+str(path))
    print('Next: cmake -S <repo> -B <repo>/build -DCMAKE_BUILD_TYPE=Release && cmake --build <repo>/build -j 8')

if __name__=='__main__':main()
