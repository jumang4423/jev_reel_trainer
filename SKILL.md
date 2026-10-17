---
name: jev-reel-trainer
description: Train an Instagram Reels feed toward chosen content (dogs, cats, other animals) without following, reposting, commenting, DM-ing or saving — using a local vision model as a Jev-style decision engine. Use when the user asks to make their Reels show more of a kind of video, to fill a feed with animals/dogs/cats, to crawl or seed Instagram for reels, to judge reel frames locally (llama.cpp + Qwen3-VL), to run the jev_reel_trainer crawler on their real logged-in Brave, or to rotate varied search themes (puppy → speedy cat → weird bugs) so each run surfaces different content. Also handles the Jev-compatible cloud path (Vercel AI Gateway / TypeSafe Jev) when paid credits exist.
---

# jev-reel-trainer

Instagram のリールを**あなたの代わりに視聴して**、フィードを狙った内容（犬・猫・その他の動物）に寄せるスキル。
判定エンジンは **ローカル VLM を Jev 風に使う**（llama.cpp + Qwen3-VL、テキストを生成させず
1 トークンの logprobs を読むだけ）ので、**無料・通信なし**で回せる。

リポジトリ: `~/PROJECTS/hacking/jev_reel_trainer`

## 核心原則（これを外すと意味がなくなる）

1. **他人に影響する操作は一切しない。** フォロー / リポスト / コメント / DM / 共有は禁止。
   これらは相手に通知が飛ぶか、他人の画面に出る。
2. **保存（Save）もしない。** ユーザーの「保存済み」コレクションを汚す。
3. **Like は「小さい投稿」だけ**（`--interact like`）。IG のリールは**いいね数を表示しない**ので、
   コメント数とリポスト数で判定する（既定 50/50 以下）。
4. **視聴時間が最強の信号。** Mosseri が1位に挙げた watch time は、匿名・通知なし・
   自分のリストを汚さない。だから**対象は最後まで見て、対象外は2秒以内に切る**。
5. **フォローしないので供給は構造化できない。** 代わりに **検索エンジンで実在のリール URL を収穫**
   し、**投稿者プロフィールを辿って**供給を増やす（これが唯一効く入口）。

## 前提

```bash
cd ~/PROJECTS/hacking/jev_reel_trainer
./serve_local.sh          # 別ターミナル。llama-server を :8081 で起動（モデル約3.1GB常駐）
```

- モデル: `~/models/Qwen3-VL-4B-Instruct-GGUF`（Apache-2.0）。無ければ `huggingface-cli download` で取得。
- ブラウザ: **ユーザーの実 Brave に CDP 接続**する（`--cdp`）。事前に一度だけ
  `brave://inspect/#remote-debugging` で「Allow remote debugging」を ON にしてもらう。
- クラウド Jev を使う場合のみ `AI_GATEWAY_API_KEY` が必要（**無料枠対象外＝有料**）。

## エージェントの仕事: 毎回ちがうクエリを考える

メモリー（`memory/`）に履歴があるので、**同じ検索を繰り返さないこと**が仕事の一部。

```bash
# 1. メモリーを見る（使ったテーマ／未使用テーマ／pending／処理済みリール数）
./.venv/bin/python crawl.py --plan

# 2. 未使用テーマから 3〜6 個えらび、クエリを積む
#    テーマは散らす（犬 → 猫 → 昆虫 → 水槽 …）。例:
./.venv/bin/python crawl.py \
  --add-query "site:instagram.com reel corgi" \
  --add-query "site:instagram.com reel jumping spider" \
  --add-query "site:instagram.com reel axolotl"

# 3. 実行（判定はローカル Jev、完走＋たまに Like）
./.venv/bin/python crawl.py --cdp --judge local --interact like --session-minutes 20
```

`--plan` の `unused_themes` が候補プール（犬/猫/小動物/鳥/野生/昆虫/水棲/牧場/横断 の ~55 テーマ）。
**カテゴリをまたぐ**と、ユーザーの言う「puppy → speedy cat → weird bugs」が実現する。

多様な動物を流したいときは `--targets any`（既定は `dog,cat`。`any` は犬猫以外も完走させる）。

## メモリーの中身

| ファイル | 役割 |
|---|---|
| `memory/queries.jsonl` | 実行済みクエリと成果（`found` / `processed` / `animal_rate_pct`） |
| `memory/pending.txt` | 次に実行するクエリ（`--add-query` が追記、`--from-memory` が消費） |
| `memory/seen_reels.json` | 処理済みリールの shortcode（**実行をまたいで重複を防ぐ**） |
| `memory/runs.jsonl` | 実行ごとのサマリー（動物率・分数） |

履歴にあるクエリは既定でスキップされる（`--allow-repeat` で解除）。
`memory/` は gitignore 済み。

## 実行モード

| コマンド | 用途 |
|---|---|
| `crawl.py --cdp --judge local` | **フィードに依存せず供給を作る（推奨）** |
| `reel_trainer.py --cdp --judge local` | いまのリールフィードを回して訓練＋**効果測定** |
| `local_judge.py --image f.jpg --context "..."` | 1 枚だけ判定して確率を見る |

測定は `runs/crawl.jsonl` と `runs/reels.jsonl` の `is_animal` を見る。
フィード側の動物率が上がっていれば効いている。

## 実機検証で判明した IG Web のクセ（必ず踏む）

1. **背景タブだと `<video>` がデコードされない。** ページ全体のスクショはグレー一色になる。
   `page.bring_to_front()` が必須。
2. **動画要素をクリックするとパーマリンクに遷移し、`...` メニューが「投稿メニュー」に化ける。**
   動画は絶対にクリックしない。前面化＋`video.muted=true; video.play()` でデコードさせる。
3. **Web のハッシュタグページは壊れている。** `/explore/tags/<tag>/` はクリックでも直リンクでも
   `/explore/search/keyword/?q=...` に化けて `Something went wrong`。**タグ経由の巡回は不可**。
4. **HTTP での検索エンジン収穫は使えない。** DuckDuckGo はすぐレート制限、Bing は `site:` を無視、
   Reddit は 403。**実ブラウザでの検索が唯一安定して動く**（Brave Search / Google はブロックされない）。
5. **キーワード検索はアカウント名を返す**（"puppy" → DJ や人物）。内容検索には使えない。
   `site:instagram.com reel <keyword>` のほうが有効。
6. **Like 済みは `aria-label` が `Unlike` に変わる。** 二重押し防止と操作検証にこれを使う。
7. **リールの `...` に「Not interested」は無い**（`Report / Go to post / Share to... / Copy link / Embed /
   About this account` のみ）。準ネガティブは Report →「I just don't like it」（既定オフ）。
8. **`Your Algorithm` / `Reset suggested content` はスマホ限定**（Web には無い）。
   これらは**無料・無害・最強**なので、ユーザーにスマホで 3 タップしてもらう価値がある。

## 実測値（このスキルの根拠）

| 経路 | 動物率 |
|---|---|
| リールフィード（技術系に偏った状態） | 0〜2% |
| **検索エンジン由来のシード** | **100%（4/4）** |
| ローカル Jev の判定 | 犬リールで `dog=1.00`、昆虫で `animal=0.9999`、人物・基板で `none=0.9999` |
| 1 フレームの判定時間 | 4.6〜7.6 秒（M1 Pro / Metal、画像エンコード込み） |

## 注意（正直に）

- **実行中は Brave のタブが前面に出る**（デコードのため）。ブラウザを触らない時間帯に回すこと。
- 検索エンジンへのクエリは **1 セッション 3〜6 件**まで。多用すると CAPTCHA のリスク。
- 投稿者プロフィール巡回は `--max-profiles`（既定 40）で制限。ページ読み込みが増えるため。
- **保証はできない。** フォローしない構成では「視聴＋たまの Like」だけなので、
  **数日〜2週間の継続**が必要。判断は `animal_rate_pct` の推移で行う。
- 自動操作は Instagram の利用規約に触れる可能性がある。自己責任で、レート制限を守って使うこと。
