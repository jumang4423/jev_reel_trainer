# jev_reel_trainer

Instagram のリールを無限にスクロールし、各リールの 1 フレームを
**TypeSafe Jev**（Vercel AI Gateway）に判定させて、対象（既定は犬・猫）なら
**Like**、それ以外はスキップ（任意で「I just don't like it」）する自動化。

判定を LLM の文章パースではなく **Jev の型付き確率**で行うのが狙い。
`experimental` ではなく、確率と確信度がそのまま返るので、しきい値で機械的に
扱える（＝「賢い if 文」をフィード訓練ループに埋め込む）。

## 構成

```
SKILL.md          エージェント向けスキル定義（このリポジトリを skill として実行する）
jev_client.py     Jev（クラウド）呼び出し — Vercel AI Gateway / 画像対応 / リトライ
local_judge.py    ローカル VLM を Jev 風に使う判定（llama.cpp の 1 トークン logprobs 読み出し）
discover.py       リールの発見 — 検索エンジン収穫（実ブラウザ）とプロフィール巡回
crawl.py          フィードに依存せず供給を作るモード＋メモリー連携
memory.py         検索クエリ／処理済みリールのメモリー（実行をまたいで重複を防ぐ）
reel_trainer.py   リールフィードを回して訓練するループ本体
serve_local.sh    ローカル判定サーバの起動
requirements.txt
.env.example
```

## セットアップ

```bash
cd jev_reel_trainer
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # 同梱 Chromium（Brave/Chrome があれば実質不要）

cp .env.example .env
# .env の AI_GATEWAY_API_KEY を埋める（Vercel > AI Gateway > API Keys）
```

`AI_GATEWAY_API_KEY` は moshi シェルに読み込んでから実行する：

```bash
export $(grep -v '^#' .env | xargs)
```

## 判定エンジンは 2 つ（クラウド Jev / ローカル VLM）

| `--judge` | 実体 | 費用 | 備考 |
|---|---|---|---|
| `jev`（既定） | Vercel AI Gateway の `typesafe-ai/jev-1.14.0` | **有料**（無料枠対象外。$0.042/1M 入力） | 本家。最も正確・最速・最安（有料の中では） |
| `local` | llama.cpp + 小型 VLM（Qwen3-VL-4B） | **無料・完全ローカル** | 通信なし。精度は落ちる |

### ローカル判定の仕組み（`local_judge.py`）

Jev と同じ「型付きの判断を確率で返す」を、**テキストを生成せずに**実現する：

1. 画像＋文脈＋「取れるラベル一覧（dog/cat/other/none）」を 1 プロンプトにする
2. `max_tokens=1, temperature=0, logprobs=true, top_logprobs=20` で**1 トークンだけ**生成
3. その位置の分布からラベルの確率を読む（無いラベルには下限を敷いて正規化）
4. `dog`/`cat`/`other`/`none` を Jev 互換の `is_dog` / `is_cat` / `is_animal` に変換

自由文の生成もパースも無いので、スキーマ外れが起きません。

### ローカルの起動

```bash
# モデル（Apache-2.0, 約 3.4 GB）
huggingface-cli download Qwen/Qwen3-VL-4B-Instruct-GGUF \
  --include "Qwen3VL-4B-Instruct-Q4_K_M.gguf" "mmproj-Qwen3VL-4B-Instruct-F16.gguf" \
  --local-dir ~/models/Qwen3-VL-4B-Instruct-GGUF

# llama-server を起動（Metal、ポート 8081）
./serve_local.sh

# 動作確認（1 枚の画像を判定）
.venv/bin/python local_judge.py --health
.venv/bin/python local_judge.py --image /path/to/frame.jpg --context "caption..."

# リールのループをローカル判定で回す（API キー不要）
.venv/bin/python reel_trainer.py --cdp --judge local
```

モデルを差し替えるときは `MODEL_DIR` / `MODEL_FILE` / `MMPROJ_FILE` を環境変数で指定する。
M1 Pro / 16 GB なら 4B クラス（Qwen3-VL-4B, Gemma 3 4B, Qwen3.5-4B, SmolVLM2-2.2B, Moondream2）が現実的。

## 供給を作る（crawl）＋メモリー

フィードは「フォロー中＋最近反応したリールに似たもの」から候補を集めるため、
反応が全部テックだと**動物は永久に来ません**。そこでフィードに依存しない入口を使う。

```bash
# 1. メモリーを見る（使ったテーマ・未使用テーマ・pending・処理済み数）
.venv/bin/python crawl.py --plan

# 2. 未使用テーマから選んでクエリを積む（テーマを散らす）
.venv/bin/python crawl.py \
  --add-query "site:instagram.com reel corgi" \
  --add-query "site:instagram.com reel jumping spider"

# 3. 実行（判定はローカル Jev。20分で自動終了）
.venv/bin/python crawl.py --cdp --judge local --interact like --session-minutes 20
```

やっていること:

1. **検索エンジン（実ブラウザ）で実在のリール URL を収穫** — タグページは Web では壊れており、
   HTTP 収穫も全滅するので、これが唯一安定する入口
2. 各リールを開いて **ローカル Jev で判定**
3. 対象なら **最後まで視聴（＋ループ）**、投稿者とハッシュタグを収穫
   → **投稿者プロフィールを次に回す**（犬アカウントなら全部犬）
4. 対象外なら**すぐ離れる**（無駄な視聴をしない）
5. `--report-every` 本ごとに**動物ヒット率**を表示

### メモリー（`memory/`、gitignore 済み）

| ファイル | 役割 |
|---|---|
| `queries.jsonl` | 実行済みクエリと成果（`found`/`processed`/`animal_rate_pct`） |
| `pending.txt` | 次に実行するクエリ |
| `seen_reels.json` | 処理済みリール（**実行をまたいで重複を防ぐ**） |
| `runs.jsonl` | 実行ごとのサマリー |

履歴にあるクエリは既定でスキップ（`--allow-repeat` で解除）。
**エージェントが `--plan` を見て毎回違うクエリを考える**ことで、
「puppy → speedy cat → weird bugs」のように多様な内容が流れる。

### Like ポリシー

IG のリールは**いいね数を表示しない**ので、**コメント数とリポスト数**で判定する。
`--interact like` のときだけ、**両方が閾値以下（既定 50）の投稿のみ** Like する。
フォロー / リポスト / コメント / DM / 保存は**実装していない**。

## 既存の Brave セッションを使う（推奨・ログイン不要）

Chrome / Brave / Edge は **136 以降、既定プロファイルへの `--remote-debugging-port` を
無視する**（Cookie 窃取対策）。`playwright-ext` の拡張機能モードは CDP を使わないので
この制限を回避しているが、**Python からはそのセッションを直接使えない**。

代わりに **「認可付きリモートデバッグ」** を使う。あなたの Brave（Chromium 154 相当）なら
これが使える:

1. Brave のアドレスバーで `brave://inspect/#remote-debugging` を開き、
   **「Allow remote debugging」を ON** にする（一度だけ。`Local State` に保存され、再起動後も有効）
2. 実行する:

```bash
.venv/bin/python reel_trainer.py --cdp --probe
```

- `--cdp`（= `auto`）はプロファイル直下の `DevToolsActivePort` を読み、
  `ws://127.0.0.1:<port><path>` に接続する。この経路は HTTP の `/json/version` が
  404 になるが、**Playwright は `ws://` に直接接続できる**ので問題ない（検証済み）。
- **接続時に Brave に承認ダイアログが出るので「許可」を押す**（1 実行につき 1 回）。
- **あなたの開いているタブは触らない**（新しいタブを 1 枚作って使う）。終了時もブラウザは閉じない。
- 明示指定もできる: `--cdp ws://127.0.0.1:9222/devtools/browser/<uuid>`

## 代替: 専用プロファイル（初回だけ手動ログイン）

`--cdp` を使わない場合は、専用プロファイル（`.browser-profile/`）で Brave が起動する。
この場合は Instagram に一度手動ログインが必要（Cookie は以後残る）。プロファイルは
リポジトリに含めない（.gitignore 済み）。

```bash
.venv/bin/python reel_trainer.py --probe
```

## 実行

```bash
# 無限ループ（既定）。犬・猫なら Like、それ以外はスキップ
python reel_trainer.py

# 20 本だけ、ネガティブは Report -> I just don't like it
python reel_trainer.py --steps 20 --negative report

# 全動物を対象にする
python reel_trainer.py --targets any --threshold 0.85

# フレームを保存しつつ様子見
python reel_trainer.py --frames-dir frames/ --steps 10
```

主なオプション：

| オプション | 既定 | 意味 |
|---|---|---|
| `--steps` | `0` | 処理するリール数。`0` は無限 |
| `--cdp [URL]` | なし | 既存ブラウザに CDP 接続（ログイン不要）。`--cdp` だけで `auto` 検出 |
| `--targets` | `dog,cat` | Like する対象。`any` で全動物 |
| `--threshold` | `0.90` | Like を押す確率しきい値 |
| `--negative` | `skip` | 対象外のときの挙動。`report` で Report→「I just don't like it」 |
| `--max-reports` | `30` | 1 実行あたりの report 操作上限 |
| `--max-likes-per-hour` | `120` | Like のレート制限 |
| `--min-delay` / `--max-delay` | `2.5` / `7.0` | 次リールまでの待機秒（ランダム） |
| `--dry-run` | off | 判定も操作もせずループだけ回す |
| `--probe` | off | 1 本だけ判定して詳細表示 |
| `--log` | `runs/reels.jsonl` | 判定・行動の JSONL ログ |

## Jev への質問

1 リクエストで 3 つを**並列**評価する（`POST https://ai-gateway.vercel.sh/v1/decisions`）：

```json
{
  "model": "typesafe-ai/jev-1.14.0",
  "input": [{"role": "user", "content": [
    {"type": "input_text", "text": "Instagram のリール…キャプション: ..."},
    {"type": "input_image", "image_url": "data:image/jpeg;base64,..."}
  ]}],
  "questions": [
    {"type": "predicate", "name": "is_dog", "instructions": "画像にはっきりと犬が写っていますか？"},
    {"type": "predicate", "name": "is_cat", "instructions": "画像にはっきりと猫が写っていますか？"}
  ]
}
```

`--targets any` のときだけ `is_animal` も足す。確率 `>= --threshold` なら Like。

## 実機で判明した Instagram Web リールのクセ（この実装の根拠）

1. **背景タブだと `<video>` がデコードされない。** ページ全体のスクショは
   グレー一色になる。`page.bring_to_front()` が必須。
2. **動画要素をクリックするとパーマリンク（`/reels/<code>/`）に遷移し、
   `...` メニューが「投稿メニュー」に化ける。** よって動画は絶対にクリックしない。
   前面化 + `video.muted = true; video.play()` でデコードさせる。
3. **要素スクショより canvas の `drawImage` のほうが良い。** 要素スクショには
   再生ボタンのオーバーレイが乗ることがある。
4. **Web 版リールの `...` に「Not interested」は無い。**
   実際の中身は `Report / Go to post / Share to... / Copy link / Embed / About this account`。
   準ネガティブは `Report` →「I just don't like it」（既定はオフ）。
5. **Like 済みは `aria-label` が `Unlike` に変わる。** これを二重押し防止と
   操作成功の検証に使う。

## 既知の制約

- **Jev は AI Gateway の無料枠では使えない（実測）。** Hobby の月 $5 無料クレジットを
  持っていても `typesafe-ai/jev*` は対象外で、画像つきの判定は
  `HTTP 403 / no_providers_available（Free tier users do not have access to this model）`
  になる。**AI Gateway のクレジット購入（有料 tier 化）が必須**。
  なお無料枠の対象モデルに vision 対応モデルは 1 つも無いため、無料のままでは
  画像判定はどのモデルでもできない。
- **Jev の画像入力は実験的機能**（TypeSafe のプレビュー）。精度はテキストより落ちる。
  1 フレームだけで判定するので、動画の途中にしか動物が出ないリールは取りこぼす。
  必要なら `--frames-dir` で複数フレームを保存し、複数枚を同時に送る拡張が効く。
- **動画・音声は送れない**（Jev は静止画のみ）。キャプションはテキストで渡している。
- **`Not interested` が無い**ため、ネガティブ信号は実質「Report → I just don't like it」か
  「即スキップ（滞在時間）」しかない。
- **Like の上限**は Instagram 側の制限がある。`--max-likes-per-hour` を守ること。
- 自動操作は **Instagram の利用規約に触れる可能性**がある。自己責任で。

## 次の拡張（推奨）

- **確信度フォールバック**：Jev の確信度が低いときだけ vision LLM
  （例 `google/gemini-3.7-flash`、`openai/gpt-6-luna`）で再判定する。
  `providerMetadata.typesafe.confidence` を読めば分岐できる。
- **複数フレーム**：`--frames-dir` を利用し、3 枚程度をまとめて 1 リクエストで送る。
- **判定の記録と評価**：`runs/reels.jsonl` をラベル付けして精度を測る。
