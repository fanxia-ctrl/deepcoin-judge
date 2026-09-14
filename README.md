# deepcoin-judge

按 theme-tip rubric 判语音客服的单轮回答，并用回收的人工标注算人机一致率。

judge 是独立的一套东西：它不关心被测 agent 长什么样，只吃「一轮问答 + 材料」。
接一个新数据源 = 在 `judge/adapters.py` 里加一个 loader。

**LLM 客户端已实现（Dify / OpenAI 兼容），只差凭证。** 复制 `config.env.example`
成 `config.env` 填好就能开跑；`--llm mock` 可以不花钱跑通全链路。

## 一分钟上手

```bash
python3 cli.py selftest                      # 自检，不发请求
python3 cli.py truth data/labels/xxx.xlsx    # 从标注抽权威口径
python3 cli.py check  <跑批目录>              # 在真实数据上预检
python3 cli.py run    <跑批目录>              # 判一遍，顺带出复核表
python3 cli.py sheet  data/runs/<run>          # 单独补导复核表 xlsx
python3 cli.py agree  data/runs/<run>/judge.jsonl data/labels/xxx.xlsx
```

`<跑批目录>` 目前支持 voice agent 的 `bench/runs/<run_id>`（里面要有 `turns.jsonl`），
或任意对齐了字段的 jsonl（`--source jsonl`）。

## 判据表 v2

5 个主题 14 条判据，其中 8 条是**条件型** —— 同一次判断既出负向命中，也出正向比率。
外加 11 条规则（7 条契约卡点 · 3 条计分 · 1 条簇级），不调模型。

```
T1 事实与权威口径冲突   needs query answer truth       1 条
T2 切题与完整性        needs query answer             5 条
T3 个人账户类问题缺取证  needs query answer signals      1 条
T4 角色与话术越界       needs query answer             4 条
T5 证据使用           needs query answer evidence signals  3 条
```

`python3 cli.py rubric` 打印完整表。判据定义在 `judge/rubric.py`，改判据只改那里。

### 三条设计原则

**一个观察只记一次。** 同源判据合并，计分**主题内取最大值**再跨主题相加 ——
v1 里 `neg_3_1 + neg_3_2` 这类对子几乎总一起亮，一个行为被记两次就从「需修改」跳到「不可上线」。

**判据放在它需要的材料那一组。** `needs` 既决定 bundle 分组，也决定缺材料时不判 ——
没有权威口径时 T1 输出「无法判定」，不退化成拿证据当口径。

**正向不另判。** 带 `applies_when` 的判据多输出一个 `applies`（前提是否成立），
做对 = 前提成立且未命中，直接得到澄清率、取证率、兜底率这类比率。
比率比「加分总和」可解释，做版本对比用它。

## 输入输出

**输入**，每条五样材料：原始问题 · 回答 · 本轮召回切片 · 权威口径 · 机器信号。
不是每个主题都吃全部五样，缺了就不判。

**输出**，每条两层。逐判据：

```json
{"code":"neg_3_1","hit":true,"applies":true,"quote":"您这单手续费明显偏高",
 "why":"没索要订单号就断言账户事实"}
```

整条汇总：`verdict` / `deduct` / `bonus` / 六个比率 / `contract_ok` /
`hard_fail` / `neg_hits` / `undecidable`。

## 计分

1. 任一 `hard_fail` 命中 → 直接不可上线，不看累计分
2. 否则 `deduct = Σ主题 max(命中判据权重)`；> 2.0 不可上线，> 0 需修改，= 0 可直接发
3. 契约规则 R1–R7 独立卡点，不进 deduct —— 质量分再高也不能发
4. 条件型标记出加分与比率，只在合格候选里排序，不抵扣负向
5. 缺材料**只推翻「干净」这个结论**：已经查出来的问题照算，但不能说可直接发

## 分组（`--group`）

**默认 `one`：一次把五样材料和全部 14 条判据发出去，一次拿回 14 条 verdict。**

| 模式 | 每轮调用 | 94 轮 | 用在哪 |
|---|---:|---:|---|
| `one` | 1 | 94 | **默认** |
| `bundle` | 4 | 376 | 按材料需求合并，做对照用 |
| `theme` | 5 | 470 | 一主题一次，调 prompt 时定位问题用 |

后两种是对照组：如果一次判完出现「判到后面全判不命中」或判据互相污染，
分组跑一遍就能确认是不是这个原因。换模式会自动失效旧缓存。

## 目录

```
cli.py                 命令行入口
judge/
  rubric.py            判据表，唯一真源
  prompts.py           按组拼 prompt（含 applies / 分类字段）
  llm.py               Dify + OpenAI 兼容客户端，另带 mock
  adapters.py          数据源 → 统一 Turn
  context.py           组装五样材料
  pipeline.py          并发、缓存、重试、引用校验
  score.py             门禁与计分
  report.py            run 级报告
  sheet.py             复核表 xlsx（一行一 case，只列命中）
  agreement.py         人机一致率
  make_truth.py        标注 J 列 → 权威口径
  preflight.py         预检
  selftest.py          自检
  scorers/objective.py 规则层 R1–R11
data/labels/           标注表与 truth.jsonl
data/runs/<run>/       judge.jsonl · report.md · 复核表.xlsx · agreement.md · cache.jsonl
```

## 三件没做完的事

**「证据够不够」还没有标签。** C 档里零召回只占 5/50，90% 的错误发生在**有召回**的情况下，
所以要判的是「召回的切片里有没有答案」。`neg_5_1` 的入口档与措辞档、`neg_4_2/4_3/4_4`
都还是 n=0，权重按机制暂定。下一批标注表要把这一遍做成必填列。

**工具记录字段没接。** `turns.jsonl` 里还没有本轮调了哪些工具，R9/R10 不触发，
`neg_3_1` 目前以 LLM 判定为主。上游补上字段后自动生效。

**阈值没校准。** `DEDUCT_LIMIT = 2.0` 是 v1 沿用值。接真模型后在这 95 条上调，
使 judge 判死率贴近人判 C 率（52%），再冻结。
