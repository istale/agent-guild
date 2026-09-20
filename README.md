# Agent Collaboration Hub

一個公會式的 agent 協作平台：一張客訴 = 一個房間，客服 agent 接件，
查不到答案就找內部專家的 agent，或把委託貼上布告板讓人搶，
而你（系統開發者）在 `/ops` 全程看得到。

```text
外部客戶 ──/chat──► 平台（房間 + 黃頁 + 布告板 + 答案庫）◄──► 客服 agent
                         │                                      │
                         │                              指名 @ 或 公開委託
                         ▼                                      ▼
                  你 · /ops 監控                        內部 domain agent
                                                        （各自有人類協作者）
```

十個檔案:

| 檔案 | 做什麼 |
|---|---|
| `registry_server.py` | 平台本體:掛上 directory、rooms、knowledge 與兩個網頁 |
| `room.py` | 房間、客訴、guest token、公開委託、搶單、過期與升級 sweep |
| `knowledge.py` | 答案庫:解決過的問題自動入庫,支援英文詞幹與中文 bigram |
| `directory.py` | 黃頁:用 skill 找誰能做 |
| `web.py` | `/chat`（客戶）與 `/ops`（你） |
| `identity.py` | did:key + Ed25519 + 人對 agent 的委任憑證 |
| `agent_card.py` | agent card（OASF 風格的 skill) |
| `envelope.py` | 平台的入場檢查（**不是** A2A) |
| `agents.py` | 客服 agent 與內部 domain agent |
| `connect.py` / `client.ts` | 接線模組:Python / TypeScript（Pi） |

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

`GET /openings` 看板,`POST /rooms/{id}/openings/{utt}/claim` 搶單——
第一個成功的擁有它,其他人拿到 **409** 就放手。搶到會在房間留下 NOTICE。

**布告板會自己整理**（讀取時 lazy sweep,不需背景任務),三件事都留下 NOTICE:

| 情況 | 平台做什麼 | 環境變數（秒） |
|---|---|---|
| 搶到卻沒回答 | 委託**回板**,誰也不會永久卡著它 | `HUB_CLAIM_TTL`（120） |
| 一直沒人接 | 提高優先度,`/ops` 出現 `priority` 徽章 | `HUB_ESCALATE_AFTER`（60） |
| 更久還是沒人 | 標記 `needs_human`,`/ops` 出現紅色徽章 | `HUB_HUMAN_AFTER`（180） |

**`to` 只有真人的 @ 會被自動解析**,agent 必須明確指定對象。這不是潔癖:
agent 會互相引用彼此的回覆,轉述時如果把原文裡的 `@Some Agent` 又解析成新指派,
兩個 agent 會無限互踢（這個 bug 真的發生過,而且會讓第一則轉述被當成內部訊息、
客戶看不到）。

## 答案庫（戰利品）

內部 agent 每次回答一個指名給它的問題,平台就把 **客戶的原話 + 那個答案** 存起來
（`knowledge.py`）。客服在升級之前會先查庫,命中就自己答,並註明原本是誰判斷的:

```text
第 1 張單  客戶:發票被重複扣款  → 客服 → @Billing Hermes → Mei 的 agent 回答
第 2 張單  客戶:卡片有重複扣款  → 客服直接答（引用上次的結論）,沒驚動 Mei
```

實測:兩張說法不同的單,Mei 的 agent 只被問過 **1 次**,第二張的回應快了一個來回。

- 入庫是**平台寫的**,不是 agent 自稱「這是知識」;每筆都留 agent 與人類的名字,
  所以錯的答案追得到來源
- 比對做了輕量詞幹（`charge` ↔ `charged`）與 **中文 bigram**（`tokenize` 只認
  latin,中文否則整段被丟掉）。至少要兩個關鍵詞重疊才算命中,一個是巧合
- 客服的門檻是 `--kb-threshold`（預設 0.4,設 0 就關掉）
- `/ops` 看得到每筆答案、來源、以及**被重用幾次**

> 沒有時效機制:退款政策改了,舊答案還是會被引用。要嘛加 TTL,
> 要嘛讓人類協作者能在 `/ops` 上把某筆標記為過期——目前兩者都沒有。

## 客服情境（最主要的跑法）

一張客訴 = 一個房間。外部客戶用 guest token 在裡面講話（沒有金鑰、沒有 DID），
客服 agent 接單,遇到不懂的就**去 directory 查誰能處理**並在房間裡 @ 他,
內部 domain agent 回答,客服再轉述給客戶。你（系統開發者）在 `/ops` 看全部。

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

## 跑起來

```bash
pip install -r requirements.txt

# 平台（兩台以上都連得到的機器）
python registry_server.py --host 0.0.0.0 --port 9100

# 客服 agent
python agents.py --role cs --platform http://127.0.0.1:9100

# 每個內部領域一個,各自有人類協作者
python agents.py --role domain --domain billing  --owner Mei --platform http://127.0.0.1:9100
python agents.py --role domain --domain shipping --owner Jun --platform http://127.0.0.1:9100
python agents.py --role domain --domain bug      --owner Ken --platform http://127.0.0.1:9100
```

然後開兩個視窗:客戶在 `http://127.0.0.1:9100/chat?customer=Wang` 打字,
你在 `http://127.0.0.1:9100/ops` 看全部。

想看布告板的過期與升級,把時間縮短:

```bash
HUB_ESCALATE_AFTER=4 HUB_HUMAN_AFTER=9 HUB_CLAIM_TTL=6 \
  python registry_server.py --port 9100
```

## 身份驗到哪裡為止

agent 對平台的每個寫入都會驗三件事（`envelope.open_envelope`）:

1. 信封簽章對得上 caller 宣稱的 DID,且時間戳在 5 分鐘內
2. caller 的 agent card 自簽有效
3. card 上的委任憑證（人 → agent）有效且未過期

過了這三關就照做。委任憑證裡的 `scopes` 與 skill 的 `sensitivity`
只是**宣告**,發佈在 card 上給別人讀,**程式不會拿它擋任何東西**。
要對誰保留什麼,寫在你自己的 handler 裡。

外部客戶則完全不在這套之內:他們拿平台發的 guest token,沒有金鑰、沒有身份。

## 已知邊界（要上線前補的）

- **全部只在記憶體**：平台重啟,單子、transcript 與答案庫都消失（agent 會自己
  接回來並重新註冊,但歷史不會）→ 換成 SQLite。
- **答案庫沒有時效**：政策改了,舊答案還是會被引用。
- **agent 的回答是罐頭**：`agents.py` 的 `DOMAINS[...]["answer"]` 與客服的招呼語
  都是固定字串。要接真 LLM / 真 agent 就換 `handle_customer` 與 `answer_mentions`
  裡產生文字那一行（`connect.py` 與 `client.ts` 是 `@answers` / handler)。
- **人類協作者只能看,不能插話**：Mei / Jun / Ken 在 `/ops` 讀得到自己 agent 的
  往來,但沒有「我來回這句」的入口 → 要加一個內部發言端點（可比照 guest token）。
- **`/chat` 與 `/ops` 都沒有認證**：知道 room id 就讀得到,`/ops` 更是全都看得到
  → 只綁 loopback / Tailnet。
- **房間沒有存取控制**：任何身份合法的 agent 都能 join 任何房間;要私密房間就在
  `room.join_room` 加 invite 檢查。
- **傳輸沒有加密與 replay cache**：信封有簽章與 5 分鐘時間窗,但對外請走
  mTLS 或 Tailnet。
- **比對都是關鍵詞計分**（`directory.search` 與 `knowledge.search`),不是 embedding;
  要語意搜尋就換掉那兩個函式,介面不用動。
- **Hermes 的 A2A bridge 還沒寫**,而 `envelope.py` 跟 A2A v1.0 不相容（見上）。
