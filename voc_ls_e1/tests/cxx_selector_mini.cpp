#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <list>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>
struct Bitset{size_t count() const {return 0;}};
struct LabelAdv{
 int objective=0,node=0;bool dir=true;std::vector<int> snapshot{0};Bitset bits;
 int getObjective(){return objective;} int getNode(){return node;}
 bool getDirection(){return dir;} int getUnreachableCount(){return 0;}
 std::vector<int>& getSnapshot(){return snapshot;}
 Bitset& getVisited(){return bits;}
};
struct Problem {
 int getNumNodes(){return 5;}
 std::vector<int> neighbors{1,2,3};
 std::vector<int>& getNeighbors(int node,bool direction){return neighbors;}
};
class LMDefault {
 public:
 static constexpr int E1_FEATURES=7;
 ~LMDefault();
 void e1Setup();
 std::array<double,E1_FEATURES> e1Features(LabelAdv* l);
 double e1Score(LabelAdv* l);
 void e1Trace(const std::array<double,E1_FEATURES>& x,int attempt,int accepted);
 LabelAdv* getCandidateLearned(bool f,bool b);
 Problem* problem=nullptr;
 bool e1_learned_mode=false,e1_trace_enabled=false;
 int e1_top_k=8,iterations=1,turn_forward=0,turn_backward=0;
 std::array<double,E1_FEATURES> e1_mean{},e1_scale{},e1_weights{};
 double e1_bias=0;
 std::ofstream e1_trace_stream;std::mutex e1_trace_mutex;
 std::atomic<unsigned long long> e1_candidate_calls{0},e1_scored_labels{0},e1_select_ns{0},e1_trace_rows{0},e1_reordered_choices{0};
 std::vector<std::list<std::pair<int,int>>> forward_candidates,backward_candidates;
 std::vector<LabelAdv> forward_labels,backward_labels;
 std::vector<std::vector<std::pair<int,int>>> forward_closed,backward_closed;
};


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

int main() {
 Problem problem;
 LMDefault lm;
 lm.problem=&problem;
 lm.forward_labels.resize(2);
 lm.forward_labels[0].objective=5;
 lm.forward_labels[1].objective=10;
 lm.forward_candidates.resize(5);lm.backward_candidates.resize(5);
 lm.forward_closed.resize(5);lm.backward_closed.resize(5);
 lm.forward_candidates[0].emplace_back(5,0);lm.forward_candidates[0].emplace_back(10,1);
 lm.e1_scale.fill(1.0);lm.e1_weights.fill(0.0);lm.e1_weights[0]=1.0;
 auto* x=lm.getCandidateLearned(true,false);
 if(x->objective!=10 || lm.forward_candidates[0].size()!=1 || lm.e1_reordered_choices.load()!=1)
    return 10;
 auto* y=lm.getCandidateLearned(true,false);
 if(y->objective!=5 || !lm.forward_candidates[0].empty())return 11;
 if(lm.e1_scored_labels!=3)return 12;
 std::cout<<"E1_CXX_MINI_PASS"<<std::endl;
 return 0;
}
