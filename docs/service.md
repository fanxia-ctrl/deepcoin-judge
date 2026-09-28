# judge 服务

把一批 agent 回答交给 judge 判，异步返回结论。判定逻辑和 `cli.py run` 完全一样，
这里只是在外面包了一层任务队列和 HTTP 接口。

    bash serve.sh                              # 服务器上：后台启动、崩了自动拉起，见「部署」
    python3 cli.py serve                       # 本机调试：前台跑，只听 127.0.0.1:8787

只用标准库，有 python3 的机器就能跑。自检：`python3 cli.py selftest`，最后一节是服务。

## 一分钟上手

```bash
# 提交：turns.jsonl 一行一轮，直接当请求体发
curl -s -X POST 'http://127.0.0.1:8787/v1/jobs?name=九月回归' \
     -H 'Authorization: Bearer <key>' -H 'Content-Type: application/x-ndjson' \
     --data-binary @data/in/shane-fx-0908/turns.jsonl
# → 202 {"id": "20260928-121700-10f683", "status": "queued", "warnings": [...], ...}

# 轮询
curl -s http://127.0.0.1:8787/v1/jobs/20260928-121700-10f683 -H 'Authorization: Bearer <key>'
# → {"status": "running", "progress": {"done": 31, "total": 94}, "summary": {...}, ...}

# 取结果
curl -s http://127.0.0.1:8787/v1/jobs/20260928-121700-10f683/results -H 'Authorization: Bearer <key>'
```

**要等多久。**默认配置（3 采样表决）下约 **150 轮/小时**，瓶颈是后面那一台 vLLM，
不是服务。94 轮约 35~40 分钟。任务之间排队，前面有大任务，后面的要等。

## 接口

| | |
|---|---|
| `POST /v1/jobs` | 提交一批轮次，回 `202` + 任务 |
| `GET /v1/jobs` | 我提交过的任务，新的在前，`?limit=` 默认 20 |
| `GET /v1/jobs/{id}` | 状态、进度、排队位置、汇总 |
| `GET /v1/jobs/{id}/results` | 逐轮结果，按提交顺序，没判完的是 `null` |
| `GET /v1/jobs/{id}/report` | `report.md`，和 `cli.py run` 产出的一样 |
| `GET /v1/jobs/{id}/sheet` | `复核表.xlsx`，发给标注的就是这张 |
| `POST /v1/jobs/{id}/cancel` | 取消 |
| `GET /v1/rubric` | 当前判据表和判据指纹 |
| `GET /healthz` | 存活；`?upstream=1` 真打一次模型，上游不通回 `503` |

所有接口除 `/healthz` 都要 `Authorization: Bearer <key>`。

## 提交

两种写法，二选一：

```jsonc
// Content-Type: application/json
{"turns": [ {...}, {...} ], "options": {"samples": 3, "name": "九月回归"}}
```

```text
Content-Type: application/x-ndjson，一行一轮；选项放 query string：?samples=1&name=xxx
```

一次最多 2000 轮。输入有错整批退回 `400`，消息里写明第几轮哪个字段。

### 每一轮

| 字段 | 必填 | 说明 |
|---|---|---|
| `query` | 是 | 用户的问题 |
| `voice` / `detail` | 至少一个 | 回答。只有一段回答的话直接传 `answer`，当作 `voice` |
| `case_id` | | 不传按顺序编 `t0001`…，传了不能重复 |
| `evidence_text` | **强烈建议** | 模型这一轮**实际看到的**证据原文（KB 切片 + 工具返回） |
| `kb_hits` / `kb_count` / `kb_top_score` | 建议 | 召回明细。没有 `evidence_text` 时用 `kb_hits` 拼证据 |
| `route_rules` | 建议 | 模型 system prompt 里按路由固定的那段业务规则 |
| `tool_calls` / `tool_names` | | 调了哪些工具，有没有空返回 |
| `raw_answer` / `envelope_ok` | | voice agent 的 JSON 信封原文。见下面「契约规则」 |
| `truth` | | 这一轮的权威口径（人工确认的正确说法），优先级最高 |
| `suite` / `route_case` / `expect` … | | 原样进报告和复核表 |

**缺字段不报错，但会降级，提交时在 `warnings` 里说清楚：**

- 没有 `evidence_text` 也没有 `kb_hits` → T1 事实冲突、T5 证据使用两组**不判**，标「无法判定」。
  只传 `query` + `answer` 的话，14 条判据只剩 T2/T3/T4 那 10 条
- 没有 `route_rules` → 少一处口径来源，事实冲突和「该讲的规则没讲」会判得偏弱
- 没有 `truth` → T1 以证据为口径。仓库里 `data/labels/truth.jsonl` 按 `case_id` 有 70 条，
  `case_id` 对得上会自动用（`builtin_truth: false` 关掉）

**契约规则。**R1–R7 查的是 voice agent 自己的输出契约：JSON 信封、voice 不超 120 字、
逐字内容不进 voice、有终止标点……命中任何一条这一轮就不能发。不是这个 agent 的回答，
没有信封：**不传 `raw_answer` 时服务会用 `voice`/`detail` 合成一个**，信封检查（R1）
和空答检查就不会误伤，其余几条照查。

### 选项

| | 默认 | |
|---|---|---|
| `samples` | 3 | 每轮判几次，1–5 |
| `vote_k` | 过半 | 几票命中才算命中 |
| `group` | `one` | 14 条判据一次判完（`one`），或按主题拆开判（`theme` / `bundle`） |
| `temperature` | 0 | |
| `retries` | 1 | 单次调用解析失败 / 调用失败时重试几次 |
| `evidence_chars` | 4000 | 证据截多长 |
| `builtin_truth` | true | 用不用仓库里的权威口径 |
| `name` | | 任务名，进报告抬头 |

默认值就是 `docs/bench.md` 的协议，91% 那个数就是这样量出来的。`samples=1` 快三倍，
但同配置重跑 F1 能摆 6 分，**单采样的结果别拿去和 bench 比**。

## 任务

```jsonc
{
  "id": "20260928-121700-10f683",
  "status": "running",              // queued / running / done / failed / cancelled
  "progress": {"done": 31, "total": 94},
  "queue_position": null,           // 排队时：前面还有几个任务
  "note": null,                     // 上游挂了暂停时写在这里：暂停多久、最后一次报错
  "warnings": ["2/94 轮没带 evidence_text ……"],
  "error": null,                    // failed / cancelled 的原因
  "rubric": "787213ce782f",         // 判据指纹
  "endpoint": ["glm-5.3@vllm-0.28.0-tp8-cdd3e961"],   // 端点指纹
  "summary": {"verdicts": {"不可上线": 12, "需修改": 11, "可直接发": 6}, "incomplete": 2,
              "hits": {"neg_5_1": 14, "neg_1_1": 9}},
  "options": {...}, "created": "...", "started": "...", "finished": "...", "links": {...}
}
```

**两个指纹要跟着结果一起存。**判据文本改了、端点换了模型，同样的输入会判出不同的结果。
隔几周回头对比两批数时，先确认这两个值一样。

## 结果

`GET /v1/jobs/{id}/results` 默认精简：

```jsonc
{
  "case_id": "online:case_03_rebate:008",
  "verdict": "不可上线",             // 可直接发 / 需修改 / 不可上线 / 判定不完整
  "complete": true,                 // false = 有判据组没判成，结论不可信
  "deduct": 5.8, "hard_fail": ["neg_1_1"],
  "neg_hits": ["neg_1_1", "neg_2_2"],
  "hits": [{"code": "neg_1_1", "name": "与口径冲突", "quote": "实时到账功能无法更改",
            "why": "口径说在资产-返佣页面可开启……", "fact_type": "规则结论"}],
  "contract_ok": true, "contract_hits": [], "scored_rules": [],
  "undecidable": [], "unjudged": [], "judge_errors": []
}
```

- `?full=1`：原样返回判定行，带证据、每条判据的全部投票，和 `judge.jsonl` 一行一样
- `?format=jsonl`：一行一轮，适合直接落文件

## 取消

排队中的直接取消。在跑的会在当前几轮判完后停下，已判完的轮次保留，
`report` / `sheet` 只含判完的部分。

## 上游不稳时

后面只有一台 vLLM，挂过一小时，并发 4 时也掉过连接。服务的处理：

- **模型没调通的轮次不算判完。**先暂存，这一遍结束后重判，一个任务最多判 3 遍，
  还调不通才按「判定不完整」落盘。不这样的话，上游抖一下，那几轮就永久作废了
- **连续 5 轮调不通 = 上游挂了。**任务放回队列，已判完的保留，暂停 1 → 2 → 5 → 10 分钟再试，
  不空转。任务的 `note` 和 `/healthz` 的 `paused_until` / `last_outage` 会写明
- **服务重启不丢任务。**跑到一半的任务回到队列，只补判没结果的轮次；
  问过模型的请求有缓存，不会重复花调用

## 部署

在另一台服务器上，只用 `serve.sh` 这一个脚本：

```bash
git clone git@github.com:fanxia-ctrl/deepcoin-judge.git && cd deepcoin-judge
bash serve.sh check      # 第一次先跑：自检 + 真调一次模型端点，确认这台机器连得上
bash serve.sh            # 启动
```

要求：Python 3.9+，`curl`。其余都是标准库；想要复核表 xlsx 再 `pip3 install openpyxl`。

| 命令 | |
|---|---|
| `bash serve.sh` | 启动。已经在跑就什么都不做，可以反复执行 |
| `bash serve.sh stop` / `restart` | 停止 / 重启（改了 key、拉了新代码之后） |
| `bash serve.sh status` | 在不在跑 + 健康检查 |
| `bash serve.sh logs` | 跟日志（`data/service/serve.log`，超过 50MB 自动轮转一份） |
| `bash serve.sh attach` | 进 tmux 看实时输出，`Ctrl-b` 再按 `d` 退出，服务不停 |

默认监听 `0.0.0.0:8787`、并发 4。要改就在命令前加环境变量：
`JUDGE_PORT=9000 JUDGE_WORKERS=2 bash serve.sh`。

### 调用方的 key

`service.keys`，一行一个 `名字:密钥`。**第一次启动时如果没有，会自动生成一个 `default`**，
`cat service.keys` 就能看到。换成自己的、或者给每个调用方加一行，然后 `bash serve.sh restart`：

```text
default:第一次启动时自动生成的随机串
voice-agent:换成你自己定的密钥
```

调用方每个请求带 `Authorization: Bearer <密钥>`。名字只用来区分任务归属，
各自只看得见自己提交的任务。这个文件已在 `.gitignore`、权限 600，不会进仓库。

它和 `config.env` 里的 `JUDGE_API_KEY` 是两回事：那个是 judge 去调**模型端点**用的，
这个是别人来调 **judge 服务**用的。

### 进程怎么不被杀

脚本已经做了前两层，第三层需要你在服务器上配一次：

1. **关掉 SSH 不退**：有 tmux 就开在 tmux 会话 `deepcoin-judge` 里；没有就用 `setsid nohup` 脱离终端
2. **崩了自己起来**：会话里是一个重启循环，Python 进程退出 5 秒后自动拉起
3. **机器重启、tmux 本身被杀**：`crontab -e` 加两行，不需要 root：

   ```cron
   @reboot      cd /path/to/deepcoin-judge && bash serve.sh >/dev/null 2>&1
   */5 * * * *  cd /path/to/deepcoin-judge && bash serve.sh >/dev/null 2>&1
   ```

   第二行每 5 分钟巡检一次：在跑就什么都不做，不在跑就拉起来。
   cron 的 `PATH` 很短（一般只有 `/usr/bin:/bin`），python3 或 tmux 装在别处（conda、`/usr/local/bin`）
   的话要写全路径：`PYTHON=/opt/conda/bin/python3 PATH=/usr/local/bin:$PATH bash serve.sh`。
   `which python3 tmux` 看一眼就知道要不要加。

有 root 的话，也可以不用 tmux 和 crontab，直接交给 systemd（`Restart=always` 管崩溃，
`enable` 管开机），脚本的 `run` 子命令就是给它用的，前台跑、不自己循环：

```ini
# /etc/systemd/system/deepcoin-judge.service
[Unit]
Description=deepcoin-judge 异步服务
After=network-online.target

[Service]
WorkingDirectory=/path/to/deepcoin-judge
ExecStart=/usr/bin/env bash serve.sh run
Restart=always
RestartSec=5
User=<跑服务的用户>

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now deepcoin-judge
journalctl -u deepcoin-judge -f        # 看日志
```

两种方式二选一，别同时开，会抢端口。

**不管哪种，停掉或重启都不丢任务**：跑到一半的任务下次启动时接着跑，已判完的轮次不重判。

### 其他

- **网络**：服务器要能直连 `config.env` 里的 `JUDGE_BASE_URL`（现在是裸 IP `162.62.213.254:8000`）。
  `bash serve.sh check` 会真调一次，不通会直接报出来。服务器上有系统代理的话，确认 `JUDGE_NO_PROXY=1`
- **防火墙**：对外开 8787（或你改的端口）
- **数据**：`data/service/` 下是任务库 `jobs.sqlite`、模型缓存 `cache.jsonl`、每个任务的产物 `jobs/<id>/`，
  已在 `.gitignore`。缓存只增不删，定期清
- **并发**：`JUDGE_WORKERS` 是全局上限，所有任务共用。上游只有一台 vLLM，调大只会更多掉连接
- **凭证**：模型端点的 key 在 `config.env` 里、跟着仓库走，clone 下来就能用。
  正式对外之前建议移出仓库、改成环境变量（环境变量优先，`config.env` 只在没设时补），并轮换一次

## 还没做的

- 同步接口（判一轮等结果）：单轮 3 采样要 30~60 秒，不适合同步挂着
- 任务优先级 / 按调用方配额：现在严格先来先判
- 缓存和任务产物的自动清理
