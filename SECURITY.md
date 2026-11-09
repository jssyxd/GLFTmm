# SECURITY — 凭证泄露处置（必读）

## 事实

2026-11-09 审计发现：以下**活的交易凭证**曾以明文提交进本仓库历史：

| 凭证 | 曾出现的路径 | 分支 |
|---|---|---|
| HL API agent 私钥 `0xd6d4…d585` | `02_rwa_tokenized_equity_mm/core/hyperliquid_sndk_live.py` | `main` |
| 同上 | `core/legacy/ant_cross_arb_{live,v2,v3_ws}.py`、`scripts/buy_quota.py`、`scripts/smoke_test_ws_engine.py` | `tree-twin` |
| OKX API key / secret / passphrase | 同 `core/legacy/*` 三个文件 | `tree-twin` |
| Kaggle token `KGAT_ac40…0d40` | `.herd-swarm/decisions.md` | `tree-twin` |
| HuggingFace token `hf_LLoo…wBE` | 早期提交（已被 GitHub push protection 拦截并改写） | 两个分支 |

**当前工作树已清零**（明文替换为 `<*_FROM_ENV>` 占位符）。但 **git 历史仍含这些字符串** ——
删除文件或改写当前内容**不能**使已泄露的凭证恢复安全。

## 必须执行（按顺序，由账户持有人操作）

1. **立刻轮换 HL API agent**：在 Hyperliquid 前端作废 `0x4956aD…9da7c` 这个 agent 授权，重新生成新 agent。
2. **立刻轮换 OKX API key**：作废 `776e25a5-…`，新建 key 并重设 IP 白名单。
3. **轮换 Kaggle / HuggingFace token**。
4. 确认轮换后，本仓库历史中的旧字符串即成为无效载荷，可不做 `filter-repo` 重写（重写会破坏所有 clone 与 PR）。
5. 若后续需要公开仓库：仍建议用 `git filter-repo --replace-text` 清史，且**必须在轮换之后**做。

## 代码侧规则（已实施）

- 所有凭证从环境变量读取：`HL_AGENT_PRIVATE_KEY` / `OKX_API_KEY` / `OKX_SECRET_KEY` / `OKX_PASSPHRASE`
- `.gitignore` 已忽略 `.env`
- `04_preipo_cross_arb/check_branch_purity.py` 之外，建议在 CI 增加 secret 扫描（gitleaks / trufflehog）

## 教训

"先把密钥写进代码跑通，之后再清理"在本仓库已实际发生过一次。
任何 `git add` 之前，凭证必须只在环境变量或 `.env`（已忽略）里。