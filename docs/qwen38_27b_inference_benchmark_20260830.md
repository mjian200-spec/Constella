# qwen3.8-27b 推理调用实验报告（vllm vs sglang，2026-08-30）

## 一句话结论

**最优调用策略**：用 sglang 0.5.18 启动 qwen3.8-27b 时开启模型自带 MTP 投机解码（`--speculative-algorithm NEXTN`）并把 mamba 状态缓存从默认 40 槽放大到 160 槽（`--max-mamba-cache-size 160 --mamba-ssm-dtype bfloat16`，投机参数 2 步 / topk 1 / 2 草稿 token），48 并发规则/概念抽取实测 **956 out-tok/s（≈1.1 req/s，p50 ≈26s）**，是 sglang 默认配置（332 out-tok/s）的 **2.9 倍**、vllm 0.19 基线（453 out-tok/s）的 **2.1 倍**。vllm 0.19 侧 MTP 同样实测通过：**833 out-tok/s（基线 453 的 1.84 倍，达 sglang 峰值 956 的 87%）**，无需 Docker、更省运维。

## 环境

- GPU：2× RTX PRO 6000 Blackwell（sm_120，各 ~97GB），驱动 580.126.18（CUDA 13.0）；GPU 0 被生产 vllm 服务占用 ~90GB，本实验全部用 GPU 1。
- vllm 0.19.0：宿主环境直接 `vllm serve`，端口 8003（生产服务，`--max-num-seqs 48`）。
- sglang 0.5.18：Docker 镜像 `sglang-22.04-cu130`（Ubuntu 22.04 + CUDA 13.0 + torch 2.13），端口 8000/8011，`--network host --gpus device=1`。
- 模型：`/DATA/jm/llms/qwen3.8-27b`。**稠密混合架构，不是 MoE**（`Qwen3_5ForConditionalGeneration`：64 层 = 48 层 linear_attention + 16 层 full_attention，`mtp_num_hidden_layers: 1` 自带 MTP 头；safetensors 55.6GB bf16 ≈ 27.8B 参数，无 `num_experts`）。

## 负载与测试方法

- 负载：200 条真实规则抽取请求（`/tmp/sglang-build/bench_load.json`，取自项目样本），OpenAI `/v1/chat/completions` 协议，temperature=0、关闭 thinking、max_tokens 2048。
- 客户端并发 48（信号量保活，一轮全部跑完）。
- 协议差异说明：sglang 基线两轮用 4 轮 ×96 请求（max_tokens 4096），MTP 轮次用 1 轮 ×200（max_tokens 2048）；平均输出长度相近（888 vs 860/874 token），横向可比。

## 结果

| 引擎与配置 | 请求数 | 总耗时 | req/s | out-tok/s | p50 | 平均输出 |
|---|---|---|---|---|---|---|
| vllm 0.19 基线（8003，生产配置） | 200 | 706s | 0.54 | 453 | 42.8s | ~890 |
| sglang 基线（8011，默认缓存 40 槽） | 384 | 1028s | 0.37 | 332 | 68.3s | 888 |
| sglang 基线 + prefill CUDA graph 开关 | 384 | 1034s | 0.37 | 324 | 68.6s | 872 |
| sglang + MTP（默认缓存，并发锁 8） | 200 | 434s | 0.46 | 396 | 65.3s | 860 |
| **sglang + MTP + 大缓存（最优）** | 200 | **183s** | **1.09** | **956** | **25.8s** | 874 |
| vllm 0.19 + MTP（8003，`num_speculative_tokens 2`） | 200 | 206s | 0.97 | 833 | 28.3s | 858 |

最优配置延迟分布：p90 37.7s、p95 45.7s、p99 63.6s、max 67.4s。

## 关键发现

1. **模型是稠密混合架构，非 MoE**；decode 批处理下权重带宽只占 HBM ~30%（453 tok/s ÷ 48 并发 ≈ 9.4 step/s × 55.6GB ≈ 524GB/s vs ~1.8TB/s），带宽从未饱和，瓶颈在 linear_attention kernel 实现效率。
2. **sglang 基线落后的真正原因**：混合模型每请求占 5 个 mamba 状态槽，默认 `max_mamba_cache_size=40` → 并发锁死 8。这是 sglang 332 与 vllm 453 差距的主要来源，与官网宣发能力无关。
3. **MTP 投机解码有效**：模型自带 MTP 头，sglang `--speculative-algorithm NEXTN`（解析为 EAGLE，topk=1）以 `Qwen3_5ForCausalLMMTP` 类型额外加载 5.53GB 草稿权重；实测 accept len ~3、accept rate 0.7~0.9，draft 开销极小（draft 1.46s vs target verify 14.49s）。
4. **显存公式（易踩坑）**：投机解码中间状态预留 = 每请求状态 ×（并发上限+1）× 草稿 token 数；默认 `--mem-fraction-static 0.92` 下 240 槽会报 `ValueError: Loaded weights leave no GPU memory for the KV cache`。最优组合 160 槽 + bf16 状态 + 2 草稿 token，把并发上限提到 32（客户端仍 48，服务端排队）。
5. **投机参数必须全显式或全自动**：只设 `--speculative-num-draft-tokens` 会触发断言（`speculative_eagle_topk is None`）。
6. **vllm 0.19 MTP 实测通过**：`--speculative-config '{"method": "mtp", "num_speculative_tokens": 2}'`（qwen3_next_mtp/qwen3_5_mtp 均并入 mtp，启动时确认 "Detected MTP model. Sharing target model embedding weights"，草稿层复用目标模型 embedding/lm_head）。**`num_speculative_tokens` 必须显式给出**，否则 pydantic 报错 "num_speculative_tokens must be provided with speculative model"；该参数也不存在独立 CLI flag（vllm 0.19 改用 config-group help）。实测 833 out-tok/s，为 vllm 基线 453 的 1.84 倍、sglang 峰值 956 的 87%；vllm 会警告 `num_spec_tokens > 1` 时同一 MTP 层会多次 forward，接受率略低于 sglang 侧。

## 最优启动命令

### sglang 0.5.18（已验证，GPU 1，端口 8000）

```bash
docker run -d --name sglang-bench --gpus device=1 --network host \
  -v /DATA/jm/llms:/DATA/jm/llms sglang-22.04-cu130 \
  -m sglang.launch_server \
  --model-path /DATA/jm/llms/qwen3.8-27b \
  --served-model-name /DATA/jm/llms/qwen3.8-27b \
  --host 0.0.0.0 --port 8000 \
  --max-running-requests 48 \
  --mem-fraction-static 0.92 \
  --speculative-algorithm NEXTN \
  --max-mamba-cache-size 160 \
  --mamba-ssm-dtype bfloat16 \
  --speculative-num-steps 2 \
  --speculative-eagle-topk 1 \
  --speculative-num-draft-tokens 2
```

启动日志确认项：`max_running_requests is capped to 32 by the mamba state cache`（预期），`The server is fired up and ready to roll!` 后即可调用。`--served-model-name` 需与客户端 `model` 参数一致。

### vllm 0.19（已验证，GPU 0，端口 8003）

```bash
vllm serve /DATA/jm/llms/qwen3.8-27b \
  --served-model-name /DATA/jm/llms/qwen3.8-27b \
  --host 127.0.0.1 --port 8003 \
  --max-num-seqs 48 \
  --speculative-config '{"method": "mtp", "num_speculative_tokens": 2}'
```

启动日志确认项：`Detected MTP model. Sharing target model embedding weights`、`speculative_config=SpeculativeConfig(method='mtp', num_spec_tokens=2)`。

### 验证请求

```bash
curl http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "/DATA/jm/llms/qwen3.8-27b",
  "messages": [{"role": "user", "content": "你好"}],
  "temperature": 0,
  "max_tokens": 64
}'
```

sglang 端口 8000、vllm 端口 8003，其余参数相同。

## 遗留风险与待办

- **8003 生产配置决策**：vllm MTP 已实测通过（833 out-tok/s），当前 8003 无服务在跑；是否保留 MTP 配置重启生产待用户决定。
- **E2E 流水线计时未完成**：两次尝试均未跑完——第一次用 sglang 容器，stage 1 开始 11s 后容器被外部 SIGQUIT 干净终止（来源不明），LLM 调用全部 HTTPError，结果无效；第二次用 vllm MTP（服务正常、GPU 0 利用率 98%），stage 1 中被用户主动中止。计时脚本与配置齐备（`/tmp/constella_e2e/run_e2e.sh` + `configs/bench_sglang/`），随时可重跑。
- bf16 mamba 状态缓存下的输出质量未与 vllm 输出做对比验证，正式切换 sglang 生产前建议用项目校验脚本抽查。
- 基准脚本与负载：`/tmp/sglang-build/bench_extraction.py`（注意 `--rounds 1` 会用全部 200 条样本）、`bench_load.json`；结果文件 `bench_sglang_result.txt` / `bench_mtp_round1.txt` / `bench_mtp_bigcache.txt`。
- 测试容器 `sglang-bench` 已删除（GPU 1 释放），后续若重跑 sglang 侧需按上方命令重建。
