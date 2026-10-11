"""Three-way cost and token accounting benchmark.
Compares:
  A. Current customized agent-authored skill (local agent writes memory bundles).
  B. Pinned original Claude-Mem (local background worker inference).
  C. Proposed Hermes Cloud Bridge (native passive hooks + cloud observer + compact bounded retrieval).
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.retrieval import estimate_tokens


class BenchmarkSimulator:
    """Simulates multi-turn session workloads across the three configurations."""

    def __init__(self, sample_tasks: Optional[List[Dict[str, Any]]] = None):
        self.sample_tasks = sample_tasks or [
            {
                "turn": 1,
                "user_prompt": "Refactor the database schema to support UUID project keys.",
                "tools_used": ["view_file", "replace_file_content", "run_tests"],
                "assistant_response": "Refactored schema.sql with UUID project keys and verified 12 tests pass.",
                "observations_produced": 3,
            },
            {
                "turn": 2,
                "user_prompt": "Implement milestone completion verification endpoint in server/api.py.",
                "tools_used": ["view_file", "replace_file_content", "run_tests"],
                "assistant_response": "Implemented PUT /milestones endpoint with explicit admin acceptance check.",
                "observations_produced": 2,
            },
            {
                "turn": 3,
                "user_prompt": "Run full test suite and verify SQLite FTS5 search performance.",
                "tools_used": ["run_command", "view_file"],
                "assistant_response": "Full test suite passed in 5.2s. SQLite FTS5 rank queries verified.",
                "observations_produced": 2,
            },
        ]

    def run_config_a_agent_authored(self) -> Dict[str, Any]:
        """Configuration A: Active agent authors exact memory JSON and imports via worker."""
        # Baseline overhead: Instructions in system prompt (~9.7 KB AGENTS.md memory block)
        agents_md_prompt_overhead = estimate_tokens("X" * 9700)  # ~2400 tokens per turn
        total_local_in = 0
        total_local_out_useful = 0
        total_local_out_memory = 0
        turns_data = []

        cumulative_bundle_chars = 0
        for task in self.sample_tasks:
            # Local agent receives prompt + system overhead
            prompt_tokens = estimate_tokens(task["user_prompt"])
            tool_tokens = sum(estimate_tokens(t) * 15 for t in task["tools_used"])
            turn_in = agents_md_prompt_overhead + prompt_tokens + tool_tokens

            # Useful task output
            useful_out = estimate_tokens(task["assistant_response"])

            # Memory authoring output: agent must format and emit the JSON bundle
            bundle = {
                "turn_id": f"turn-{task['turn']}",
                "prompt": task["user_prompt"],
                "observations": [
                    {
                        "type": "change" if i % 2 == 0 else "feature",
                        "title": f"Obs {i} for {task['assistant_response'][:30]}",
                        "narrative": "Detailed narrative explaining verified changes and findings in the codebase.",
                        "facts": ["Fact 1", "Fact 2"],
                        "files_modified": ["server/api.py", "server/db.py"],
                    }
                    for i in range(task["observations_produced"])
                ],
                "summary": {
                    "request": task["user_prompt"],
                    "completed": task["assistant_response"],
                    "next_steps": "Continue with next milestone.",
                },
            }
            bundle_str = json.dumps(bundle, ensure_ascii=False)
            cumulative_bundle_chars += len(bundle_str)
            memory_out = estimate_tokens(bundle_str)

            total_local_in += turn_in
            total_local_out_useful += useful_out
            total_local_out_memory += memory_out

            turns_data.append({
                "turn": task["turn"],
                "in_tokens": turn_in,
                "out_useful": useful_out,
                "out_memory_overhead": memory_out,
            })

        return {
            "config": "A: Customized Agent-Authored Skill",
            "local_agent_input_tokens": total_local_in,
            "local_agent_useful_output_tokens": total_local_out_useful,
            "local_agent_memory_output_tokens": total_local_out_memory,
            "local_agent_total_output_tokens": total_local_out_useful + total_local_out_memory,
            "cloud_observer_tokens": 0,
            "total_tokens_consumed": total_local_in + total_local_out_useful + total_local_out_memory,
            "local_memory_overhead_percent": round(100 * total_local_out_memory / (total_local_out_useful + total_local_out_memory), 2),
            "turns": turns_data,
        }

    def run_config_b_upstream_claude_mem(self) -> Dict[str, Any]:
        """Configuration B: Pinned original Claude-Mem with local background worker model."""
        # Hook capture: no agent-authored JSON output, but background model processes each event
        hook_instruction_overhead = 150  # Hook instructions injected
        total_local_in = 0
        total_local_out_useful = 0
        total_bg_worker_tokens = 0
        turns_data = []

        for task in self.sample_tasks:
            prompt_tokens = estimate_tokens(task["user_prompt"])
            tool_tokens = sum(estimate_tokens(t) * 15 for t in task["tools_used"])
            turn_in = hook_instruction_overhead + prompt_tokens + tool_tokens
            useful_out = estimate_tokens(task["assistant_response"])

            # Local background worker runs inference per tool event and turn end
            bg_in = prompt_tokens + tool_tokens + useful_out + 500
            bg_out = 300 * task["observations_produced"]
            total_bg_worker_tokens += (bg_in + bg_out)

            total_local_in += turn_in
            total_local_out_useful += useful_out

            turns_data.append({
                "turn": task["turn"],
                "in_tokens": turn_in,
                "out_useful": useful_out,
                "bg_worker_tokens": bg_in + bg_out,
            })

        return {
            "config": "B: Original Claude-Mem (Local Worker)",
            "local_agent_input_tokens": total_local_in,
            "local_agent_useful_output_tokens": total_local_out_useful,
            "local_agent_memory_output_tokens": 0,
            "local_agent_total_output_tokens": total_local_out_useful,
            "cloud_observer_tokens": total_bg_worker_tokens,
            "total_tokens_consumed": total_local_in + total_local_out_useful + total_bg_worker_tokens,
            "local_memory_overhead_percent": 0.0,
            "turns": turns_data,
        }

    def run_config_c_hermes_cloud_bridge(self) -> Dict[str, Any]:
        """Configuration C: Hermes Cloud Bridge (passive hook capture + cloud observer on VM + bounded retrieval)."""
        # Compact context banner injected only on turn 1 (~350 tokens) and deduplicated thereafter
        context_injected_tokens = 350
        total_local_in = 0
        total_local_out_useful = 0
        total_cloud_observer_tokens = 0
        turns_data = []

        for idx, task in enumerate(self.sample_tasks):
            prompt_tokens = estimate_tokens(task["user_prompt"])
            tool_tokens = sum(estimate_tokens(t) * 15 for t in task["tools_used"])
            # Injected only initially on turn 1
            injected = context_injected_tokens if idx == 0 else 0
            turn_in = injected + prompt_tokens + tool_tokens
            useful_out = estimate_tokens(task["assistant_response"])

            # Cloud observer runs on GCP VM in batches at turn completion
            obs_in = prompt_tokens + tool_tokens + useful_out + 250
            obs_out = 150 * task["observations_produced"]
            total_cloud_observer_tokens += (obs_in + obs_out)

            total_local_in += turn_in
            total_local_out_useful += useful_out

            turns_data.append({
                "turn": task["turn"],
                "in_tokens": turn_in,
                "out_useful": useful_out,
                "cloud_observer_tokens": obs_in + obs_out,
            })

        return {
            "config": "C: Hermes Cloud Bridge",
            "local_agent_input_tokens": total_local_in,
            "local_agent_useful_output_tokens": total_local_out_useful,
            "local_agent_memory_output_tokens": 0,
            "local_agent_total_output_tokens": total_local_out_useful,
            "cloud_observer_tokens": total_cloud_observer_tokens,
            "total_tokens_consumed": total_local_in + total_local_out_useful + total_cloud_observer_tokens,
            "local_memory_overhead_percent": 0.0,
            "turns": turns_data,
        }

    def run_full_benchmark(self) -> Dict[str, Any]:
        """Run all 3 configurations and compute comparative metrics."""
        cfg_a = self.run_config_a_agent_authored()
        cfg_b = self.run_config_b_upstream_claude_mem()
        cfg_c = self.run_config_c_hermes_cloud_bridge()

        local_agent_tokens_a = cfg_a["local_agent_input_tokens"] + cfg_a["local_agent_total_output_tokens"]
        local_agent_tokens_c = cfg_c["local_agent_input_tokens"] + cfg_c["local_agent_total_output_tokens"]

        local_token_reduction = round(100 * (local_agent_tokens_a - local_agent_tokens_c) / local_agent_tokens_a, 2)

        return {
            "configurations": {
                "config_a": cfg_a,
                "config_b": cfg_b,
                "config_c": cfg_c,
            },
            "summary_comparison": {
                "local_agent_tokens": {
                    "config_a": local_agent_tokens_a,
                    "config_b": cfg_b["local_agent_input_tokens"] + cfg_b["local_agent_total_output_tokens"],
                    "config_c": local_agent_tokens_c,
                },
                "local_memory_overhead_on_agent_output": {
                    "config_a": f"{cfg_a['local_memory_overhead_percent']}%",
                    "config_b": "0%",
                    "config_c": "0%",
                },
                "local_agent_token_reduction_vs_a": f"{local_token_reduction}%",
                "cloud_observer_workload": {
                    "config_a": 0,
                    "config_b": cfg_b["cloud_observer_tokens"],
                    "config_c": cfg_c["cloud_observer_tokens"],
                },
            },
        }


def format_markdown_report(bench_result: Dict[str, Any]) -> str:
    """Format benchmark results into Markdown table report."""
    cfgs = bench_result["configurations"]
    ca, cb, cc = cfgs["config_a"], cfgs["config_b"], cfgs["config_c"]
    comp = bench_result["summary_comparison"]

    lines = [
        "# Hermes Second-Brain Three-Way Cost & Token Simulation",
        "Synthetic estimates from fixed assumptions; not measured provider usage or demonstrated savings.",
        "",
        "| Metric | Config A (Agent-Authored Skill) | Config B (Upstream Claude-Mem) | Config C (Hermes Cloud Bridge) |",
        "| :--- | :---: | :---: | :---: |",
        f"| **Local Agent Input Tokens** | {ca['local_agent_input_tokens']:,} | {cb['local_agent_input_tokens']:,} | {cc['local_agent_input_tokens']:,} |",
        f"| **Local Agent Useful Output** | {ca['local_agent_useful_output_tokens']:,} | {cb['local_agent_useful_output_tokens']:,} | {cc['local_agent_useful_output_tokens']:,} |",
        f"| **Local Agent Memory Output (Overhead)** | {ca['local_agent_memory_output_tokens']:,} | 0 | 0 |",
        f"| **Local Memory Overhead % on Output** | **{ca['local_memory_overhead_percent']}%** | 0.0% | **0.0%** |",
        f"| **Observer / Background Tokens** | 0 | {cb['cloud_observer_tokens']:,} | {cc['cloud_observer_tokens']:,} |",
        f"| **Total Local Agent Tokens** | {comp['local_agent_tokens']['config_a']:,} | {comp['local_agent_tokens']['config_b']:,} | **{comp['local_agent_tokens']['config_c']:,}** |",
        "",
        f"> Under these assumptions, the simulated local token reduction for C versus A is **{comp['local_agent_token_reduction_vs_a']}**. A live workload and provider token accounting are required to validate this estimate.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sim = BenchmarkSimulator()
    res = sim.run_full_benchmark()
    print(format_markdown_report(res))
    out_file = Path(__file__).parent / "benchmark_report.json"
    out_file.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(f"\nSaved raw JSON benchmark results to {out_file}")
