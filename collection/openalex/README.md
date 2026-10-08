# OpenAlex 城乡规划文献采集

本目录独立于 WoS、PubMed、DBLP 采集流程。脚本按 OpenAlex Topics 分类体系检索城乡规划相关主题，保留 OpenAlex 的主题、子领域、领域层级，并把主题标签随每条文献保存。

## 筛选口径

- 只保留语言为英文（OpenAlex `language=en`）的记录。
- 排除 `review`、`editorial`、`letter`、`book-review`、`correction`、`retraction`、`news`、`peer-review`、`other` 等非研究性类型。其余类型（如 article、proceedings-article、book-chapter）保留。
- 主题筛选词位于 `topic_selection.json`。首次运行会下载并保存 OpenAlex 全量 Topics 层级到 `data/openalex_topics.json`，同时生成候选主题表 `data/selected_topics.csv`；运行前可检查候选项并按需要编辑筛选词。
- 默认最多使用 10,000 API credits。每次成功请求读取 OpenAlex 响应头中的 `X-RateLimit-Credits-Used` 并累计到 `data/credit_usage.json`；达到项目累计上限后停止。采集前先估算分页请求量，超过剩余额度时会缩小主题批次，仍超限则暂停。

## 运行

在项目根目录运行，先设置自己的 OpenAlex API key（不要把密钥写进脚本或提交到版本库）：

```powershell
$env:OPENALEX_API_KEY = "你的 API key"
python collection/openalex/collect.py
```

输出写入本目录的 `data/`：

- `openalex_topics.json`：OpenAlex 预置 Topic，以及所属 subfield、field、domain 标签和 ID。
- `selected_topics.csv`：按本项目筛选词匹配到的候选城乡规划主题，供检查与调整。
- `works.jsonl`：通过筛选的文献，一行一篇，包含标题、摘要、DOI、年份、来源、作者、OpenAlex 原始类型、primary topic 和全部主题层级标签。
- `run_state.json`：游标、已处理主题批次及文献计数，可中断后继续。
- `credit_usage.json`：项目累计 API 积分账本，防止多次续跑突破 10,000 上限。
- `run.log`：运行日志。

如需从头重跑，先备份再删除 `data/run_state.json` 和 `data/works.jsonl`。`OPENALEX_MAX_CREDITS` 可设为低于默认上限的值用于小规模试跑；脚本不会允许其超过 10,000。

API 文档：[认证和额度](https://help.openalex.org/api/authentication/)、[计费示例](https://help.openalex.org/access/example-costs/)。OpenAlex 的额度按 credits 计算，过滤列表请求通常每次 1 credit；真实用量以每个响应头为准。
