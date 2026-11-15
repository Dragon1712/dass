#!/usr/bin/env python3
"""Offline, fail-fast, reversible E1 instrumentation patch for Dragon1712/dass PathWyse.

Only changes four local C++ files; preserves all core dominance, join, DSSR code.
Each insert is anchored to upstream source text, so incompatible revisions fail closed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

SUBDIR = Path("src/core/algorithms/dynamic_programming/PW_default")
FILES = ["LM_default.h", "LM_default.cpp", "PW_default.h", "PW_default.cpp"]
TAG = "VOC_LS_E1_PATCH_V1"


def replace_once(body: str, old: str, new: str, desc: str) -> str:
    count = body.count(old)
    if count != 1:
        raise RuntimeError(f"Incompatible PathWyse source at {desc}: expected anchor exactly once, found {count}")
    return body.replace(old, new, 1)


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


H_METHODS = r'''
    // VOC_LS_E1_PATCH_V1: experimental, non-pruning label reordering.
    static constexpr int E1_FEATURES = 7;
    bool e1TraceEnabled() const { return e1_trace_enabled; }
    std::array<double, E1_FEATURES> e1Features(LabelAdv* label);
    void e1Trace(const std::array<double, E1_FEATURES>& features, int attempted, int accepted);
    LabelAdv* getCandidateLearned(bool forward, bool backward);
    void e1Setup();
    double e1Score(LabelAdv* label);
'''

H_STATE = r'''
    // VOC_LS_E1_PATCH_V1 runtime policy: parsed once from the process environment.
    bool e1_learned_mode = false;
    bool e1_trace_enabled = false;
    int e1_top_k = 8;
    std::array<double, E1_FEATURES> e1_mean{};
    std::array<double, E1_FEATURES> e1_scale{};
    std::array<double, E1_FEATURES> e1_weights{};
    double e1_bias = 0.0;
    std::ofstream e1_trace_stream;
    std::mutex e1_trace_mutex;
    std::atomic<unsigned long long> e1_candidate_calls{0};
    std::atomic<unsigned long long> e1_scored_labels{0};
    std::atomic<unsigned long long> e1_select_ns{0};
    std::atomic<unsigned long long> e1_trace_rows{0};
    std::atomic<unsigned long long> e1_reordered_choices{0};
'''

CPP_IMPL = r'''
// VOC_LS_E1_PATCH_V1: keep canonical PathWyse candidate lists as the only source of truth.
// No separate priority queue: prevents stale entries, duplicate expansions, or dangling pointers.
LMDefault::~LMDefault(){
    if(e1_trace_stream.is_open()) e1_trace_stream.flush();
    std::cerr << "E1_STATS"
              << " policy=" << (e1_learned_mode ? "learned" : "baseline")
              << " candidate_calls=" << e1_candidate_calls.load()
              << " scored_labels=" << e1_scored_labels.load()
              << " selector_ns=" << e1_select_ns.load()
              << " trace_rows=" << e1_trace_rows.load()
              << " reordered_choices=" << e1_reordered_choices.load()
              << " iterations=" << iterations
              << " top_k=" << e1_top_k << std::endl;
}

void LMDefault::e1Setup(){
    const char* policy = std::getenv("E1_POLICY");
    const std::string mode = policy ? policy : "baseline";
    if(mode != "baseline" && mode != "learned")
        throw std::runtime_error("E1_POLICY must be baseline or learned");
    e1_learned_mode = mode == "learned";
    const char* topk = std::getenv("E1_TOP_K");
    if(topk && *topk){
        char* end = nullptr;
        long value = std::strtol(topk, &end, 10);
        if(!end || *end || value < 1 || value > 64)
            throw std::runtime_error("E1_TOP_K must be an integer between 1 and 64");
        e1_top_k = static_cast<int>(value);
    }
    if(e1_learned_mode){
        const char* model_file = std::getenv("E1_MODEL");
        if(!model_file || !*model_file)
            throw std::runtime_error("E1_MODEL is mandatory when E1_POLICY=learned");
        std::ifstream model(model_file);
        std::string marker;
        int nfeatures = 0;
        if(!(model >> marker >> nfeatures) || marker != "E1LINEAR_V1" || nfeatures != E1_FEATURES)
            throw std::runtime_error("Invalid E1 model header or feature count");
        for(double& x : e1_mean) if(!(model >> x) || !std::isfinite(x))
            throw std::runtime_error("Invalid E1 model means");
        for(double& x : e1_scale) if(!(model >> x) || !std::isfinite(x) || x <= 0)
            throw std::runtime_error("Invalid E1 model scales");
        for(double& x : e1_weights) if(!(model >> x) || !std::isfinite(x))
            throw std::runtime_error("Invalid E1 model coefficients");
        if(!(model >> e1_bias) || !std::isfinite(e1_bias))
            throw std::runtime_error("Invalid E1 model bias");
        std::string unexpected;
        if(model >> unexpected) throw std::runtime_error("Unexpected trailing E1 model content");
    }
    const char* trace_file = std::getenv("E1_TRACE_FILE");
    if(trace_file && *trace_file){
        if(e1_learned_mode)
            throw std::runtime_error("E1_TRACE_FILE is only allowed with baseline, for unbiased logged supervision");
        e1_trace_stream.open(trace_file, std::ios::out | std::ios::trunc);
        if(!e1_trace_stream) throw std::runtime_error("Cannot open E1_TRACE_FILE");
        e1_trace_enabled = true;
        e1_trace_stream << std::setprecision(17)
            << "iteration,objective,critical,unreachable,visited,degree,node_frac,direction,attempted,accepted\n";
    }
}

std::array<double, LMDefault::E1_FEATURES> LMDefault::e1Features(LabelAdv* label){
    const auto& snap = label->getSnapshot();
    const int node = label->getNode();
    const bool direction = label->getDirection();
    return {
        static_cast<double>(label->getObjective()),
        snap.empty() ? 0.0 : static_cast<double>(snap[0]),
        static_cast<double>(label->getUnreachableCount()),
        static_cast<double>(label->getVisited().count()),
        static_cast<double>(problem->getNeighbors(node, direction).size()),
        static_cast<double>(node) / std::max(1, problem->getNumNodes()),
        direction ? 1.0 : 0.0
    };
}

double LMDefault::e1Score(LabelAdv* label){
    auto x = e1Features(label);
    double s = e1_bias;
    for(int i=0; i<E1_FEATURES; ++i){
        const double z = std::clamp((x[i]-e1_mean[i])/e1_scale[i], -8.0, 8.0);
        s += e1_weights[i] * z;
    }
    e1_scored_labels.fetch_add(1, std::memory_order_relaxed);
    return s;
}

void LMDefault::e1Trace(const std::array<double, E1_FEATURES>& x, int attempted, int accepted){
    if(!e1_trace_enabled) return;
    std::lock_guard<std::mutex> lock(e1_trace_mutex);
    e1_trace_stream << iterations;
    for(double f : x) e1_trace_stream << ',' << f;
    e1_trace_stream << ',' << attempted << ',' << accepted << '\n';
    e1_trace_rows.fetch_add(1, std::memory_order_relaxed);
}

LabelAdv* LMDefault::getCandidateLearned(bool forward, bool backward){
    // Identify exactly the next active node from the original node policy.
    // Only reorder among the first K open labels at that node (both directions
    // if a single serial search handles both). Preserve each sorted list.
    const int n = static_cast<int>(forward_candidates.size());
    if(n <= 0) return nullptr;
    int turn = forward ? turn_forward : turn_backward;
    bool found = false;
    for(int scanned=0; scanned<n; ++scanned){
        if(turn >= n) turn = 0;
        if((forward && !forward_candidates[turn].empty()) ||
           (backward && !backward_candidates[turn].empty())){found=true;break;}
        ++turn;
    }
    if(!found) return nullptr;

    using Iter = std::list<std::pair<int,int>>::iterator;
    Iter selected{};
    bool chosen_forward = true;
    bool has_choice = false;
    double best_score = -std::numeric_limits<double>::infinity();
    int best_objective = 0;
    int best_index = 0;
    for(int idir=0; idir<2; ++idir){
        const bool dir = idir == 0; // deterministic tie preference: forward.
        if((dir && !forward) || (!dir && !backward)) continue;
        auto& lists = dir ? forward_candidates : backward_candidates;
        auto& labels = dir ? forward_labels : backward_labels;
        int count=0;
        for(auto it=lists[turn].begin(); it!=lists[turn].end() && count<e1_top_k; ++it,++count){
            const double score = e1Score(&labels[it->second]);
            const bool better = !has_choice || score > best_score + 1e-12 ||
                (std::abs(score - best_score)<=1e-12 &&
                 (it->first < best_objective ||
                  (it->first == best_objective && (dir > chosen_forward ||
                   (dir == chosen_forward && it->second < best_index)))));
            if(better){
                selected=it;
                chosen_forward=dir;
                best_score=score;
                best_objective=it->first;
                best_index=it->second;
                has_choice=true;
            }
        }
    }
    if(!has_choice) return nullptr;
    // Record whether the learned action differs from the B0 node-head choice.
    bool reference_fw = false;
    int reference_index = -1;
    int reference_cost = 0;
    if(forward && !forward_candidates[turn].empty()){
        reference_fw = true;
        reference_index = forward_candidates[turn].front().second;
        reference_cost = forward_candidates[turn].front().first;
    }
    if(backward && !backward_candidates[turn].empty() &&
       (reference_index < 0 || backward_candidates[turn].front().first < reference_cost)){
        reference_fw = false;
        reference_index = backward_candidates[turn].front().second;
    }
    if(chosen_forward != reference_fw || best_index != reference_index)
        e1_reordered_choices.fetch_add(1, std::memory_order_relaxed);
    auto& lists = chosen_forward ? forward_candidates : backward_candidates;
    auto& closed = chosen_forward ? forward_closed : backward_closed;
    auto& labels = chosen_forward ? forward_labels : backward_labels;
    closed[turn].emplace_back(*selected);
    lists[turn].erase(selected);
    if(forward) turn_forward=turn;
    if(backward) turn_backward=turn;
    return &labels[best_index];
}
'''


def patch_one(name: str, source: str) -> str:
    if TAG in source:
        raise RuntimeError(f"{name} appears already patched; do not apply twice")
    if name == "LM_default.h":
        source = replace_once(source, "#include <set>\n", "#include <set>\n#include <array>\n#include <atomic>\n#include <fstream>\n#include <mutex>\n", "LMDefault header includes")
        source = replace_once(source, "    ~LMDefault() = default;", "    ~LMDefault();", "LMDefault destructor")
        source = replace_once(source, "    LabelAdv* getCandidateNode(bool forward, bool backward);", "    LabelAdv* getCandidateNode(bool forward, bool backward);\n" + H_METHODS, "LMDefault candidate declarations")
        source = replace_once(source, "    DataCollector collector_label_fw, collector_label_bw;\n", "    DataCollector collector_label_fw, collector_label_bw;\n" + H_STATE, "LMDefault end of state")
        return source
    if name == "LM_default.cpp":
        source = replace_once(source, '#include <algorithm>\n', '#include <algorithm>\n#include <cstdlib>\n#include <chrono>\n#include <cmath>\n#include <iomanip>\n#include <iostream>\n#include <limits>\n#include <stdexcept>\n', "LMDefault includes")
        source = replace_once(source, '    iterations = 0;\n    ins_attempts_fw = ins_attempts_bw = 0;\n}', '    iterations = 0;\n    ins_attempts_fw = ins_attempts_bw = 0;\n    e1Setup();\n}', "LMDefault constructor setup")
        source = replace_once(source, "//Init\nvoid LMDefault::initLM(){", CPP_IMPL + "\n//Init\nvoid LMDefault::initLM(){", "LMDefault method definitions")
        original = '''//Returns an open candidate
LabelAdv* LMDefault::getCandidate(bool forward, bool backward) {

    LabelAdv* candidate;

    switch(candidate_type){
        default:
            candidate = getCandidateRR(forward, backward);
            break;
        case CANDIDATE_NODE:
            candidate = getCandidateNode(forward, backward);
            break;
    }

    if(candidate != nullptr)
        candidate->getDirection() ? nclosed_fw++ : nclosed_bw++;

    return candidate;
}'''
        replacement = '''//Returns an open candidate
LabelAdv* LMDefault::getCandidate(bool forward, bool backward) {
    const auto start = std::chrono::steady_clock::now();
    LabelAdv* candidate = nullptr;
    if(e1_learned_mode){
        if(candidate_type != CANDIDATE_NODE)
            throw std::runtime_error("E1 learned policy requires algo/default/candidate/type=node");
        candidate = getCandidateLearned(forward, backward);
    } else {
        switch(candidate_type){
            default: candidate = getCandidateRR(forward, backward); break;
            case CANDIDATE_NODE: candidate = getCandidateNode(forward, backward); break;
        }
    }
    const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now() - start).count();
    e1_select_ns.fetch_add(static_cast<unsigned long long>(ns), std::memory_order_relaxed);
    if(candidate != nullptr){
        candidate->getDirection() ? nclosed_fw++ : nclosed_bw++;
        e1_candidate_calls.fetch_add(1, std::memory_order_relaxed);
    }
    return candidate;
}'''
        source = replace_once(source, original, replacement, "LMDefault getCandidate method")
        return source
    if name == "PW_default.h":
        source = replace_once(source, '    void extend(LabelAdv* candidate);', '    void extend(LabelAdv* candidate, int* e1_attempted = nullptr, int* e1_accepted = nullptr); // VOC_LS_E1_PATCH_V1', 'PWDefault extend declaration')
        return source
    if name == "PW_default.cpp":
        source = replace_once(source, '''        //Get candidate label from label manager
        LabelAdv *candidate = label_manager->getCandidate(forward, backward);

        //Extend label towards reachable nodes
        collector.startTime(candidate->getDirection() ? "t_ext_fw" : "t_ext_bw");
        extend(candidate);
        collector.stopTime(candidate->getDirection() ? "t_ext_fw" : "t_ext_bw");''', '''        //Get candidate label from label manager
        LabelAdv *candidate = label_manager->getCandidate(forward, backward);
        if(candidate == nullptr) return;

        // VOC_LS_E1_PATCH_V1: trace is DISABLED during benchmark runs.
        // Collect the features BEFORE extension (no post-action information leakage).
        const bool e1_trace = label_manager->e1TraceEnabled();
        std::array<double, LMDefault::E1_FEATURES> e1_features{};
        if(e1_trace) e1_features = label_manager->e1Features(candidate);
        int e1_attempted = 0, e1_accepted = 0;
        //Extend label towards reachable nodes
        collector.startTime(candidate->getDirection() ? "t_ext_fw" : "t_ext_bw");
        extend(candidate, e1_trace ? &e1_attempted : nullptr,
                         e1_trace ? &e1_accepted : nullptr);
        collector.stopTime(candidate->getDirection() ? "t_ext_fw" : "t_ext_bw");
        if(e1_trace) label_manager->e1Trace(e1_features, e1_attempted, e1_accepted);''', 'PWDefault labeling/extend trace call')
        source = replace_once(source, 'void PWDefault::extend(LabelAdv* candidate) {', 'void PWDefault::extend(LabelAdv* candidate, int* e1_attempted, int* e1_accepted) {', 'PWDefault extend definition')
        source = replace_once(source, '''            label_manager->insert(&new_label);
            if(Parameters::getCollectionLevel()>=1)
                collector.stopTime(direction ? T_INS_FW : T_INS_BW);''', '''            if(e1_attempted) ++(*e1_attempted);
            LabelAdv* inserted_label = label_manager->insert(&new_label);
            if(e1_accepted && inserted_label) ++(*e1_accepted);
            if(Parameters::getCollectionLevel()>=1)
                collector.stopTime(direction ? T_INS_FW : T_INS_BW);''', 'PWDefault count accepted insertions')
        return source
    raise ValueError(name)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--repo',type=Path,required=True,help='PathWyse project root, e.g. ~/code/dass/pathwyse')
    g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--check',action='store_true',help='Verify all code anchors, do not modify source')
    g.add_argument('--apply',action='store_true',help='Create independent backups, patch four files')
    g.add_argument('--revert',action='store_true',help='Restore backed up originals (only if patches untouched)')
    args=ap.parse_args()
    root=args.repo.expanduser().resolve()
    if not (root/'CMakeLists.txt').is_file() and (root/'pathwyse-main'/'CMakeLists.txt').is_file():root=root/'pathwyse-main'
    target=root/SUBDIR
    if not all((target/f).is_file() for f in FILES):ap.error(f'Missing PWDefault source files in {target}')
    backup=root/'.voc_ls_e1_backup'
    if args.revert:
        mf=backup/'patch_manifest.json'
        if not mf.is_file():ap.error(f'No patch manifest: {mf}')
        j=json.loads(mf.read_text())
        for name in FILES:
            cur=(target/name).read_text()
            if hash_text(cur)!=j['files'][name]['after_sha256']:
                ap.error(f'Cannot revert {name}: source was edited after patch; manually merge with backup.')
            if hash_text((backup/name).read_text())!=j['files'][name]['before_sha256']:
                ap.error(f'Backup corrupted: {backup/name}')
        for name in FILES:shutil.copy2(backup/name,target/name)
        print('Reverted E1 source patches; old backups kept at',backup)
        return
    transformed={}
    for name in FILES:
        content=(target/name).read_text(encoding='utf-8')
        transformed[name]=(content,patch_one(name,content))
    if args.check:
        print('E1 CHECK PASS: all four PathWyse anchors recognized. Current C++ files unchanged.')
        for name,(before,after) in transformed.items():print(f'  {name}: {len(before)} -> {len(after)} bytes')
        return
    if backup.exists():ap.error(f'Backup directory already exists: {backup}; refuse to overwrite')
    backup.mkdir()
    man={'tag':TAG,'project_root':str(root),'files':{}}
    for name,(before,after) in transformed.items():
        (backup/name).write_text(before,encoding='utf-8')
        man['files'][name]={'before_sha256':hash_text(before),'after_sha256':hash_text(after)}
    (backup/'patch_manifest.json').write_text(json.dumps(man,indent=2)+'\n',encoding='utf-8')
    for name,(before,after) in transformed.items():
        (target/name).write_text(after,encoding='utf-8')
    print('E1 APPLY PASS: modified',len(FILES),'C++ files; local backup:',backup)
    print('Next: cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j 8')


if __name__=='__main__':
    main()
