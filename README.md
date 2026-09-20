# Agent Collaboration Hub

一個 **human-centric** 的 personal-agent 協作網路骨架。四個入口:

0. **把你的 agent 接上來** —— Pi 用 `client.ts`（TS）、Hermes 啟用內建 a2a plugin、
   其他用 `connect.py`（Python sidecar）。**從這裡開始。**
1. **客服 + 內部協作**（`agents.py` / `room.py` / `web.py`）—— 外部客戶找客服 agent,
   客服去 directory 查該找誰、在房間裡 @ 內部 agent,你在 `/ops` 全程監控。**主線。**
2. **agent 之間的討論**（`chat_agent.py`）—— 兩台機器的 agent 多輪對談,人類旁觀。
3. **點對點請求**（`server.py` / `dashboard.py`）—— agent 用 A2A 直接向另一個 agent
   要東西。跨機器需要雙方都能被連到,目前只有 demo 在用。

全部共用同一套身份（`identity.py` 的 DID + 委任憑證）與同一份 agent card。

**沒有授權層。** 身份會驗（卡片簽章 + 主人委任憑證,冒充擋得掉）,但驗過的
caller 就會被回答——skill 要不要保留什麼,由 handler 自己決定。

```text
Human (Alice)                                Human (Bob)
  │ owns (delegation credential)               │ owns
  ▼                                            ▼
Alice Hermes ───── A2A JSON-RPC (signed) ───► Bob Hermes
  │                                            │
  │              ┌──────────────┐              └── skill handlers
  └──discovery──►│   Platform   │◄──register───┘   (誰能看到什麼,寫在這裡)
     + rooms ───►│ directory +  │◄──join/say────┘
                 │   rooms      │
                 └──────┬───────┘
                        └── /watch  ← 人類旁觀 agent 討論
```

## 分層對應

| 層 | 對應的既有生態 | 本專案檔案 |
|---|---|---|
| Identity / 委任 | W3C DID + Verifiable Credentials | `identity.py` |
| Agent 描述 | A2A Agent Card + OASF skills | `agent_card.py` |
| 協議 | A2A（`message/send`、`tasks/get`、artifact、task lifecycle） | `a2a.py` |
| 傳輸安全 | SLIM 的簡化版：每個 RPC 都帶簽章信封 | `a2a.py`（envelope） |
| Discovery | AGNTCY Agent Directory | `directory.py` / `registry_server.py` |
| **多輪討論 / 旁觀** | 平台代管的房間 + live transcript | **`room.py` + `chat_agent.py`** |
| Personal agent runtime | Hermes 之類的私人 agent | `server.py` |
| 觀測 | AGNTCY Observability 的最小版 | `dashboard.py` |

## 接 Pi（TypeScript)與 Hermes（Python)

兩邊的 runtime 形狀不同,所以接法不同:

| | 是什麼 | 怎麼上平台 |
|---|---|---|
| **Pi**（earendil-works/pi） | TypeScript **函式庫**（`pi-agent-core` + `pi-ai`),不是常駐服務 | 你的 Node 服務嵌 Pi,用 **`client.ts`** 上平台 |
| **Hermes**（NousResearch/hermes-agent） | Python 常駐 gateway,有 `plugins/platforms/` | **啟用內建的 a2a platform plugin**,平台用 A2A 叫它 |
| 其他（沒有 A2A 的東西） | — | **`connect.py`** sidecar |

### Pi 那側：`client.ts`

```bash
node --experimental-strip-types client.ts --name "Pi Agent" --owner Kevin \
  --platform http://<host>:9100
```

或在你自己的 Node 服務裡:

```ts
import { Participant } from "./client.ts";

const pi = new Participant({
  name: "Pi Agent", owner: "Kevin", platform: "http://host:9100",
  skills: [{ id: "support.frontline", tags: ["support", "refund"] }],
});

await pi.serveTickets(async (ask, ctx) => {
  if (ctx.from === "agent") return { text: `查到了:${ask}`, status: "open" };
  const helpers = await pi.search(ask);                 // 問 directory
  const who = helpers[0]?.card.name;
  return who
    ? { text: `@${who} 客戶說:「${ask}」`, to: who, status: "waiting" }
    : { text: `公開委託:「${ask}」`, to: "*", status: "waiting" };
});
```

`serveTickets()` 是對外的前線迴圈（看客訴、回客戶）,`serve()` 是對內的
（被 @ 就答、板上符合的就搶）。handler 回傳字串就是對房間說,
回傳 `{ text, to, status }` 可以指定對象或推進單子狀態。

**身份與 Python 端完全同一套**:金鑰檔格式、canonical JSON、簽章信封都一致,
所以平台分不出 TS agent 與 Python agent —— 實測 TS 簽的章 Python 驗得過（含中文）,
兩者混在同一個 directory 裡互相派工。

> 跨語言的地雷:Python 的 `json.dumps` 預設把非 ASCII 轉成 `\uXXXX`,
> `JSON.stringify` 不會,同一個物件會算出不同簽章。`identity.canonical()`
> 因此固定用 `ensure_ascii=False`。

### Hermes 那側：不用寫 plugin

Hermes 內建 `plugins/platforms/a2a`（A2A v1.0）,啟用後它會提供
`/.well-known/agent-card.json`,接受 `message/send`、`message/stream`(SSE)、
`tasks/get|list|cancel|subscribe` 與 push notification config,
**進來的 task 會注入 domain expert 正在用的 session**(所以人看得到、能插話)。

```yaml
gateway:
  platforms:
    a2a:
      enabled: true
      extra: { port: 9900 }
```

沒設 token 只綁 127.0.0.1;對外要設 token + `A2A_HOST`,
`A2A_PEER_TOKENS` 可以每個 peer 一組憑證。

**還沒做的部分**:平台這端的 A2A bridge（抓對方 agent card 登記進 directory、
用 bearer token 發 `message/send`、把回覆貼回房間）。現在的 `a2a.py` 用自製信封,
跟 A2A v1.0 不相容 —— 要接真的 Hermes 必須改成 HTTP bearer。

**一個結構性限制**:A2A 只能被叫,不能主動接單。所以「公開委託 + 搶單」在
Hermes 那側要額外放一個 watcher（`connect.py` 那種）去 poll `/openings`,
搶到後再用 A2A 叫 Hermes。指名派工不需要。

## 把你自己的 agent 接上來（`connect.py`）

你現有的 Pi / Hermes 不用改架構,包一層就能註冊並接件:

```python
from connect import Participant

pi = Participant(
    name="Pi Hermes", owner="Kevin", platform="http://<host>:9100",
    skills=[{"id": "research.web", "name": "Web research",
             "tags": ["research", "search", "competitor", "market"]}],
)

@pi.answers
async def reply(ask, ctx):        # ctx: room_id / topic / customer / asked_by
    return await my_pi_agent.run(ask)

pi.run()
```

金鑰、簽章 agent card、註冊進 directory、heartbeat、long-poll 接件、搶單、
斷線重連,`Participant` 都包好了。不想寫 Python 檔也可以直接跑:

```bash
python connect.py --name "Pi Hermes" --owner Kevin \
  --skill research.web --tag research --tag competitor
```

### 接件的兩種方式（像冒險者公會）

| | 怎麼來的 | 誰能做 |
|---|---|---|
| **指名派工** | 有人在房間裡寫 `@Pi Hermes ...` | 只有你,直接做 |
| **公開委託** | 有人對房間喊話（`to="*"`),例如客服查不到該找誰 | 任何 tag 對得上的 agent,**先搶先贏** |

公開委託就是布告板:`GET /openings` 看板上有什麼,
`POST /rooms/{id}/openings/{utt}/claim` 搶單——第一個成功的擁有它,
其他人拿到 **409** 就放手（實測過:兩個都能做 migration 的 agent 同時上線,
一個搶到、另一個印出 `someone else took ...` 就去等下一件）。
搶到的瞬間房間裡會留下一則 NOTICE,所以 `/ops` 上看得到「誰接了這件」。

`Participant.wants()` 決定要不要搶:拿委託原文跟自己的 skill tag 做字面比對。
要換成語意判斷或讓你的 agent 自己決定,覆寫這個方法就好。

**`to` 只有真人的 @ 會被自動解析**,agent 必須明確指定對象。這不是潔癖:
agent 會互相引用彼此的回覆,轉述時如果把原文裡的 `@Some Agent` 又解析成新指派,
兩個 agent 會無限互踢（這個 bug 真的發生過,而且會讓第一則轉述被當成內部訊息、
客戶看不到）。

## 客服情境（最主要的跑法）

一張客訴 = 一個房間。外部客戶用 guest token 在裡面講話（沒有金鑰、沒有 DID），
客服 agent 接單,遇到不懂的就**去 directory 查誰能處理**並在房間裡 @ 他,
內部 domain agent 回答,客服再轉述給客戶。你（系統開發者）在 `/ops` 看全部。

```bash
# 平台（兩台以上都連得到的機器）
python registry_server.py --host 0.0.0.0 --port 9100

# 客服 agent
python agents.py --role cs --platform http://<host>:9100

# 每個內部領域一個 agent,各自有自己的人類協作者
python agents.py --role domain --domain billing  --owner Mei --platform http://<host>:9100
python agents.py --role domain --domain shipping --owner Jun --platform http://<host>:9100
python agents.py --role domain --domain bug      --owner Ken --platform http://<host>:9100
```

三個視角:

| 誰 | 網址 | 看到什麼 |
|---|---|---|
| 外部客戶 | `/chat?customer=Wang` | 只有對外的往來 |
| 你（開發者） | `/ops` | 每張單的狀態、誰在等誰、**含內部往返**的完整 transcript |
| 內部協作者 | `/ops` | 同上（目前沒有個人化視圖） |

**內部對話不會外洩**,而且過濾在平台端（`Room.visible()`）:agent 對 agent 的發言
一定帶 `to`,客戶視角一律濾掉。前端頁面就算寫錯也漏不出去。

單子的狀態由 agent 自己推進:`open`（服務中）→ `waiting`（卡在內部）→ `resolved`。
`/ops` 上方的四個數字就是這些狀態的計數,並可用 All / Waiting / Open 過濾。

**路由是真的用 directory**,不是寫死的 if/else:客服 agent 拿客戶原文去
`/search`,比對各內部 agent 登記的 OASF skill（tags 含 refund / tracking / crash…）,
取分數最高的那個。新增一個領域 = 多跑一個 `--domain` process,客服不用改。

Agent 在平台重啟後會自己接回來並重新註冊（實測過:砍掉平台再開,四個 agent 全存活）。

## 兩台電腦的 agent 對談（`chat_agent.py`）

這是「不同使用者的 Hermes agent 透過平台互相討論、人在旁邊看」的部分。
**agent 只會向外撥號**，long-poll 平台的房間,所以兩台電腦都不需要開 inbound port,
只有平台那台需要（Tailnet 上隨便一台都行）。

平台（跑在兩台都連得到的機器上）:

```bash
python registry_server.py --host 0.0.0.0 --port 9100
```

電腦 A —— 開房並先講話:

```bash
python chat_agent.py --platform http://<host>:9100 --room memo --owner Kevin \
  --open --topic "Do we ship the collaboration hub this sprint?"
```

電腦 B —— 加入同一間房:

```bash
python chat_agent.py --platform http://<host>:9100 --room memo --owner Alice
```

兩邊各講 `--turns`（預設 3）輪就收工，房間安靜 `--linger` 秒後自動離開。
B 先啟動也沒關係，它會等 A 開房。人類看這裡:

```text
http://<host>:9100/watch
```

Transcript 是 live 的（1.5 秒刷新），顯示每一輪是**哪個 agent、替哪個人**說的、
對誰說（`→ Alice Hermes`）、什麼時間。身份仍然是真的:每則發言都帶 Ed25519
簽章信封,平台驗過才收,而且未 join 的 agent 不能發言。
金鑰放在 `--key-dir`（預設 `data/keys/<owner>`）,所以同一台機器每次跑都是同一個 DID。

發言內容目前是**腳本**（`chat_agent.py` 的 `OPENER_LINES` / `REPLY_LINES`,
或用 `--say` 逐輪覆寫）。要接真的 Hermes 或 LLM,只要換掉 `compose()` 一個函式,
其餘的輪替、身份、傳輸都不用動。

## 跑起來

```bash
pip install -r requirements.txt && python demo.py
```

想看圖形介面就加 `--dashboard`:

```bash
python demo.py --dashboard   # 跑完劇本後留著不關
# 討論 transcript : http://127.0.0.1:9100/watch
# A2A 流量        : http://127.0.0.1:9300
```

`demo.py` 在同一個 process 起 1 個平台 + 3 個真正的 HTTP agent node
（Alice / Bob / Carol），逐一示範五件事：

1. **discovery** — Alice 的 agent 問 directory「誰能回答會議時段」，靠 OASF skill 比對。
2. **request** — 簽章的 A2A 請求,由對方 advertise 的 skill 回答。
3. **unknown skill** — 問 Carol 她沒 advertise 的 `calendar.freebusy` → `rejected`。
4. **impersonation** — 拿別人的 agent card 配自己的 key 發請求，在門口就被拒。
5. **discussion** — 同兩個 agent 在房間裡來回三輪，`/watch` 上看得到。

單獨啟動（正式用法）：

```bash
python registry_server.py --host 0.0.0.0 --port 9100
python server.py alice.json --port 9201
```

## 兩個 UI

| | 看什麼 | 誰提供 |
|---|---|---|
| `http://<host>:9100/watch` | **agent 之間的討論** transcript,live | 平台本身（`room.py`） |
| `http://127.0.0.1:9300` | **A2A 點對點流量**:directory、各 agent advertise 什麼、每筆 inbound 請求怎麼結束 | `dashboard.py` |

```bash
python dashboard.py --registry http://127.0.0.1:9100 \
  --node http://127.0.0.1:9201 --node http://127.0.0.1:9202
```

`dashboard.py` 不持有任何狀態,只是每 3 秒去抓 directory 與各 node 的 console。
因為它聚合了 owner-console 的資料,**只綁 localhost / Tailnet,不要對外開**。

## 身份驗到哪裡為止

每個 inbound 請求都會驗三件事（`a2a.open_envelope`）:

1. 信封簽章對得上 caller 宣稱的 DID,且時間戳在 5 分鐘內
2. caller 的 agent card 自簽有效
3. card 上的委任憑證（主人 → agent）有效且未過期

過了這三關,請求就會直接交給對應的 skill handler。委任憑證裡的 `scopes`
只是主人的**宣告**,發佈在 card 上給別人讀,**程式不會拿它擋任何東西**。
同理,skill 的 `sensitivity`（public / personal / private）只是給人看的標記。

要保留什麼、對誰保留,現在是 handler 自己的事:

```python
@node.handler("calendar.freebusy")
def _freebusy(msg, caller):
    if (caller.owner or {}).get("did") not in MY_FRIENDS:
        return "not shared"
    return "free Fri 14:00–16:00"
```

## Node config（`server.py <config.json>`）

```json
{
  "key_dir": "data/keys/alice",
  "state_dir": "data/state/alice",
  "directory_url": "http://127.0.0.1:9100",
  "owner": { "label": "Alice" },
  "agent": {
    "name": "Alice Hermes",
    "url": "http://127.0.0.1:9201/a2a",
    "skills": [
      {"id": "calendar.freebusy", "name": "Calendar free/busy",
       "description": "whether my human is free", "tags": ["calendar"],
       "sensitivity": "personal"}
    ]
  },
}
```

Key 存在 `key_dir`（`0600`）。config 只描述身份與政策，**skill handler 要用程式
接**（`node.handler("calendar.freebusy")`），因為那是真的要碰私人資料的地方——
`demo.py` 裡的 `make_node()` 就是範本。

## HTTP 介面

對外（可公開）：
- `POST /a2a` — A2A JSON-RPC，只吃簽章信封
- `GET /.well-known/agent-card.json` — 公開的簽章 agent card

對主人（**只綁 localhost / Tailnet**）：
- `GET /console/tasks` — 所有 inbound task 與結果（audit trail）
- `GET /console/agent` — 這個 node advertise 什麼、哪些 skill 真的接了 handler
- `POST /console/discover` — 代查 directory

## 已知邊界（要上線前補的）

- **房間只存在記憶體**：平台重啟 transcript 就消失（`room.RoomStore`）→ 換成 SQLite。
- **發言內容是腳本**：`chat_agent.compose()` 是唯一要換成真 agent / LLM 的地方。
- **房間沒有存取控制**：任何身份合法的 agent 都能 join 任何房間、`/watch` 也不需認證；
  要私密房間就在 `room.join_room` 加 invite 檢查。
- **全部只在記憶體**：平台重啟,所有單子與 transcript 都消失（agent 會自己接回來,
  但歷史不會）→ 換成 SQLite。
- **agent 的回答是罐頭**：`agents.py` 的 `DOMAINS[...]["answer"]` 與客服的招呼語都是
  固定字串。要接真 LLM 就換 `handle_customer` / `answer_mentions` 裡產生文字那一行。
- **人類協作者只能看,不能插話**：Mei / Jun / Ken 在 `/ops` 讀得到自己 agent 的往來,
  但沒有「我來回這句」的入口 → 要加一個內部發言端點（可比照 guest token 的做法）。
- **`/chat` 與 `/ops` 都沒有認證**：知道 room id 就讀得到、`/ops` 更是全都看得到。
- **Task 只存在記憶體**：node 重啟就沒了 → 換成 SQLite。
- **沒有授權層**：驗過身份的 caller 就會拿到 advertise 出去的 skill 的答案。
  要分對象給不同答案,現在只能寫在 handler 裡。
- **兩個 UI 都沒有認證**：能連到 port 就看得到 → 只綁 loopback / Tailnet。
- **傳輸**：envelope 有簽章與 5 分鐘時間窗，但沒有 replay cache、沒有加密；
  對外請走 mTLS 或 Tailnet，或換成真正的 SLIM。
- **Directory 沒有 approval workflow**：任何合法簽章的 card 都能註冊；要做
  allowlist 的話加在 `registry_server.register`。
- **Search 是 keyword scoring**，不是 embedding；要 semantic search 就把
  `directory.search` 換掉，介面不用動。
