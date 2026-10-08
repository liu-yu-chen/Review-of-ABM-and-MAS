# 运行顺序

本次改动：**扩充了 WOS 采集检索式**，补入城市 / GIS / 空间仿真词，修复城市方向学者
（Michael Batty、Nick Malleson、Alison Heppenstall、Ed Manley 等）召回不足的问题；
同时**改用词根（`*`）精简检索词**，并把**语义已漂移的 multi-agent 词汇排除在语料之外**。

依据：`AU=("Batty, M")` 在 WoS 中有 **448** 条记录，而语料里只有 **6** 篇——说明
原检索式要求 ABM 词出现在主题字段，而城市建模文献常写 "cellular automata"、
"urban simulation"、"land use model"，因而被系统性漏掉。

命中量变化：153,297（v1）→ 173,966（v2）→ 163,592（v3）→ **165,067（v4，实测）**。

**v3 的两处改动**

1. **词根化**：50 个写死的检索词压缩为 **38 个词根**（`agent model*` 一次覆盖
   model / models / modeling / modelling），覆盖面略增而请求量下降。
   WoS 只认**词尾** `*`；词中通配符会被静默忽略（`agent*base*` 只有 183 条，
   而 `agent based` 有 44,067 条），所以检索式里没有词中通配符。
   另：WoS 里连字符与空格等价（`agent-based` = `agent based` = 44,067），
   两种写法都保留只为可读性。

2. **排除漂移词汇**：检索式结尾加了
   `NOT TS=("multi-agent system*" OR "multiagent system*" OR "multi agent system*"
   OR "multi-agent framework*" OR "multi-agent architecture*" OR "multi-agent learning"
   OR "multi-agent reinforcement learning" OR "large language model agent*"
   OR "llm agent*" OR "llm-based agent*")`。
   这一条**不是可选项**：`multi-agent simulation` 里 `agent simulation` 是相邻词，
   不加排除就会从 `CORE_STEMS` 漏进来。`multi-agent simulation` / `multi-agent model`
   属于真正的 ABM 词汇，**保留**。
   实测：当前 31,319 篇语料中有 **1,901 篇（6.07%）**命中这些漂移词汇，
   典型如 "Agent-based software engineering"、"An Agent-Based Infrastructure for
   Enterprise Integration"——是软件 agent，不是 ABM。

**v4 的两处改动**

3. **知名作者定向采集**：检索式加了第四条分支
   `AU=("Batty, M" OR "Crooks, A" OR ... )`，共 **17 位**学者，该分支单独命中
   **1,999** 条。只收录"姓+名首字母能唯一确定一个人"的名字，判据是 AU= 的实测
   条数：一位学者毕生产出是几十到几百条，上千条说明是同名泛滥。因此**排除**：
   `Miller, H`(1,637)、`Brown, D`(9,872)、`Epstein, J`(1,940)、`Janssen, M`(2,374)、
   `North, M`(607)、`Farmer, J`(1,001)、`Conte, R`(746)。

   必要性实测：`AU=("Batty, M")` 有 448 条，主题检索式只命中其中 53 条，带 ABM
   锚点的仅 16 条——写了半辈子城市建模，却常用 "urban model" / "fractal city" /
   "urban scaling" 表述。另有一层更隐蔽的缺口：WoS 主题字段搜的是 **WoS 摘要**，
   而 Stage 5 判证据用的是 **OpenAlex 摘要**，两者不一致的论文会被主题检索漏掉。

4. **并行采集**：单次请求耗时约 **0.60 秒**，所以原来的顺序循环真实速度只有
   **1.66 请求/秒**——`0.21s` 的限速 sleep 根本不是瓶颈。现在改为多线程抓取
   一"窗"页面（`MAX_WORKERS=5`、`BATCH_PAGES=24`），限速器保证全局仍不超过
   **4 请求/秒**。实测单年 `TS=("agent-based")`：52 页用 **14 秒**（3.7 请求/秒），
   顺序做法约 31 秒，**快 2.2 倍**。

   注意：`MIN_REQUEST_INTERVAL` 从 `0.21` 调到 `0.25`。实测 `0.21`（4.76/秒）
   即使低于文档的 5/秒上限，在 1 秒窗口里仍会触发 **HTTP 429**，退避重试反而更慢。

---

## 一、采集

```powershell
cd <PROJECT_ROOT>\collection
python WOS.py
```

**注意**：

- `QUERY_VERSION` 已从 3 提到 **4**。脚本检测到版本变化会**自动重置年度断点**并
  重抓所有年份；这是必要的，否则已标记完成的年份会被跳过，新检索式永远不生效。
- 已备份原数据：`database/wos_backup_<时间戳>.parquet`。
- 脚本按 uid / doi / 归一化标题去重后**追加**到 `database/wos.parquet`，不会重复。
- 预计 **13–18 分钟**（约 3,300 页，限速 4 请求/秒）。
- 若撞到每日 20,000 请求上限会自动停下，**再次运行即可续抓**（逐页断点）。
- ⚠️ **WoS Starter API 不返回摘要**——实测 143,433 条里 **0 条**有摘要。语料里的
  摘要**全部**来自 Stage 2 的 OpenAlex 补全，所以 Stage 3 丢弃"无摘要"记录时，
  实际丢的是 **OpenAlex 没有摘要的论文**。
- **Stage 3 已放宽**（v4 起）：无摘要记录**不再一律删除**，只要**标题**里带强 ABM
  锚点就保留。实测 152,004 条里 55,641 条无摘要，其中 **14,045 条标题带锚点**；
  扣掉类型过滤后实际保留 **13,450 条**（Stage 3 输出 92,955 → **106,405**）。
  Batty 被删的那 15 条主要就是这一类。保留的记录带 `abstract_missing=True`，
  做主题分析时可以据此排除，但文献计量与作者分析照常计入。
  审计文件：`process/stage3_kept_no_abstract.csv`。
  要恢复旧行为：`python process\pipeline.py clean --strict-no-abstract`。

`PubMed.py` 与 `dblp.py` 的检索词未改（本次问题是城市/GIS 方向，主要在 WOS）。
这两个源进来的 multi-agent 漂移文献由 Stage 5 的 rule C 兜底剔除。
如需同步扩充，告诉我。

---

## 二、依次运行

```powershell
cd <PROJECT_ROOT>

# 1. 合并三源并去重        -> merged_literature.parquet
python process\pipeline.py merge

# 2. 补全字段（OpenAlex）  -> 原地写回 merged_literature.parquet
python process\pipeline.py enrich

# 3. 清洗（非英语/类型/无摘要）-> filtered_literature.parquet
python process\pipeline.py clean
# 4. 剔除无关期刊           -> analysis_literature.parquet
python process\filter_venues.py

# 5. 剔除误命中记录         -> 覆盖 analysis_literature.parquet
python process\hard_drop.py

# 6. 归一化                 -> analysis_ready.parquet
python process\normalize.py

# 7. DeepSeek 分类          -> analysis_literature_deepseek.parquet
python LLM\deepseek.py

# 8. 合并主表               -> master.parquet
python process\build_master.py --check-scholars

# 9. 作者分析与出图
python process\author_analytics.py --top 30
```

**第 7 步会自动跳过没有摘要的记录**（Stage 3 放宽后保留的那批）。它们只凭标题
分类会得到低置信度标签，还要各花一次 API 调用，所以脚本在写输入文件时就跳过，
留下列 `llm_skip_reason`：

| 值 | 含义 |
|---|---|
| `""`（空） | 已分类 |
| `no_abstract` | 无摘要，故意未送分类 |
| `not_classified` | 送了但没结果（失败/未跑到） |

跳过清单：`database/deepseek_skipped_no_abstract.csv`。
**做主题分析时要过滤 `llm_skip_reason == ""`**；文献计量与作者分析不需要过滤。

`pipeline.py` 三步也可一次跑完：

```powershell
python process\pipeline.py run
```

### 各步耗时（估计）

| 步骤 | 耗时 | 说明 |
|---|---|---|
| merge | 2–5 min | 本地 |
| enrich | 15–40 min | 受 OpenAlex 限速；断点续传 |
| clean | 2–3 min | 本地 |
| filter_venues | 1–2 min | 本地 |
| hard_drop | 1–2 min | 本地 |
| normalize | 1–2 min | 本地 |
| deepseek | 10–30 min | 取决于并发与语料量 |
| build_master | < 1 min | |
| author_analytics | 1–2 min | |

每步入参不是必需的——脚本无参数运行时都有合理默认值，PyCharm 里直接 Run 即可。

---

## 三、跑之前要清掉的三样东西

**① DeepSeek 断点（已加保护，但建议手动清）**

```
database\deepseek_classification_checkpoint.jsonl
```

该文件按 `row_index` 存储。语料重建后，同一个 `row_index` 指向的是**另一篇论文**。

我已加入指纹校验：检查点里每条记录都带 `doi`，载入时与当前输入逐条比对，不一致
比例超过 5% 就整体作废并提示。**但你也可以直接删掉它**，最干净。

**② `analysis_literature.parquet.bak`**

```
database\analysis_literature.parquet.bak
```

这是上一轮 Stage 5 自动留的备份。`hard_drop.py` **只在 .bak 不存在时才新建**，
所以这个旧 .bak 留着会导致新一轮不做备份。要保留新数据的回退点就删掉它。

**③ 旧图件目录**

```
figures_author\
figures_abm_schemeA\
```

不会被覆盖清理，重新出图前可自行删除。

---

## 四、产出对照

| 文件 | 内容 |
|---|---|
| `database\wos.parquet` | 扩充后的 WOS 原始表 |
| `database\merged_literature.parquet` | 三源合并去重 |
| `database\filtered_literature.parquet` | 清洗后 |
| `database\analysis_literature.parquet` | 期刊与记录双重剔除后 |
| `database\analysis_ready.parquet` | 归一化（29 列） |
| `database\analysis_literature_deepseek.parquet` | 加 DeepSeek 标注（23 列） |
| **`database\master.parquet`** | **主表（38 列）** ← 分析入口 |
| `database\author_ranking.csv` | 作者指标与排名 |
| `database\scholar_report.md` | 知名学者排名报告 |
| `figures_author\fig_author_leaderboard.png` | 发文/被引/散点三联图 |
| `figures_author\fig_author_scholars.png` | 24 位知名学者对照 |

审计文件仍在 `process\`：`stage1..stage5_*.csv`、`stage_summary.csv`。
其中 `stage5_excluded_terms.csv` 是 Stage 5 **rule C** 单独导出的漂移词汇剔除清单
（`stage5_hard_drop.csv` 里也含这些行，rule C 的 `drop_reason` 优先级最高）。

---

## 五、重点核查项

跑完后用 `build_master.py --check-scholars` 的输出确认城市方向学者是否回升：

| 学者 | 扩充前 | 期望 |
|---|---|---|
| Michael Batty | 6 | **显著上升**（WoS 有 448 条记录） |
| Nick Malleson | 26 | 上升 |
| Alison Heppenstall | 25 | 上升 |
| Ed Manley | 12 | 上升 |
| Andrew Crooks | 34 | 上升 |

如果 Batty 仍然只有个位数，说明扩的词还没覆盖他的发表阵地，需要再加期刊限定
（`SO=("ENVIRONMENT AND PLANNING B" OR "COMPUTERS ENVIRONMENT AND URBAN SYSTEMS" ...)`）。

---

## 六、已知的姓名陷阱

`hard_drop` 与作者分析都用**全名**匹配，不要退回姓氏匹配——语料里存在同名不同人：

| 姓氏 | 正确的人 | 容易混淆的人 |
|---|---|---|
| Batty | Michael Batty（城市建模） | G. David Batty（职业健康） |
| Manley | Ed Manley（行人仿真） | David Manley（住房研究） |
| Crooks | Andrew Crooks（GIS） | Kevin R. Crooks（生态） |
| Epstein | Joshua Epstein（ABM） | Jason / Rachel / Robert Epstein |

作者分析还做了两级合并：首字母折叠（`alison j heppenstall` → `alison heppenstall`）
与别名表（`nicolas malleson` → `nick malleson`）。合并前 `Alison Heppenstall` 只有
3 篇，合并后 25 篇。
