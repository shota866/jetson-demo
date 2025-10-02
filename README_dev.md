# Development Notes

## 1. 環境セットアップ

- `server/.env` に Sora 接続情報を記載します。

  ```env
  SORA_SIGNALING_URLS=ws://sora2.uclab.jp:5000/signaling
  SORA_CHANNEL_ID=buff0        # 既定のルーム ID
  SORA_CTRL_LABEL=#ctrl
  SORA_STATE_LABEL=#state
  ```

- Python 依存関係をインストールします。

  ```bash
  pip install -r server/requirements.txt
  ```

## 2. 起動

1. 状態管理サーバ（authoritative manager）

   ```bash
   python server/manager.py --room buff0 --pass pass
   ```

   `rates ctrl=+N drop=M state_sent=K ...` が 5 秒間隔で出力され、`data channel ready: #ctrl/#state` が表示されれば準備完了です。

2. 静的ファイルサーバ

   ```bash
   python server/web_ui_server.py --ui 8000 --robot 8001
   ```

   ログに `operator UI: http://0.0.0.0:8000/` / `robot viewer UI: http://0.0.0.0:8001/` が出力されます。

## 3. ブラウザでの確認

| 画面 | URL 例 | 備考 |
|------|--------|------|
| 操作用 UI | `http://localhost:8000/?room=buff0&pass=pass&debug=1` | `debug=1` で詳細ログを表示 |
| 表示用 UI | `http://localhost:8001/?room=buff0&pass=pass&debug=1` | 描画は常時 state に従う |

1. Chrome の `chrome://gamepad` でジョイスティックの認識を確認。
2. 操作用 UI をクリックしてフォーカスを当てるとゲームパッドポーリングが開始されます（`debug=1` なら Console に `gamepad polling enabled` が出ます）。
3. スティック/キーボード操作で以下を確認します。
   - `server/manager.py`：`rates ctrl=+N` が増加する（N > 0）。
   - 操作用 UI HUD：`CONNECTED` 表示になり、`ctrl:x.x/s` `state:x.xHz` `hb:xxxms` が更新される。
   - 表示用 UI：車・点群が描画され、操作と同期して動く。
   - DevTools → Network → WebSocket で `#state` フレームが流れ続けている。

## 4. デバッグメモ

- `debug=1` を付けると Console に Sora 接続状態、チャネル open/close、`sendCtrl` レート、受信バッファ長、ハートビート遅延などが出力されます。`debug=0` を付けると抑制できます。
- URL パラメータ `room`, `pass`, `signaling`, `ctrl`, `state`, `delayMs` を指定すると既定値を上書きできます（双方の UI で同じ値を使ってください）。
- 表示用 UI では `#ctrl` を登録していないため操作は受け付けず、`#state` のみ購読します。

## 5. トラブルシュート

| 症状 | 確認ポイント |
|------|---------------|
| HUD が `DISCONNECTED` のまま | manager の `data channel ready` が出ているか。`room` / `signaling` / `#ctrl` / `#state` が両 UI・サーバで一致しているか。 |
| `rates ctrl=+0` のまま | 操作用 UI Console で `#ctrl ready` / `sendCtrl rate` が表示されるか。`chrome://gamepad` で入力検出されているか。 |
| `hb_age` が急激に増える | ハートビートが欠落。ネットワーク遅延やブラウザタブ休止を確認。 |
| Viewer の描画が真っ黒 | `robot_ui/assets` が配信されているか。DevTools の Network タブで 404 が出ていないか。 |
| 自動再接続しない | Console に `connect:error` が出ている場合はシグナリング URL / 認証情報を再確認。 |

## 6. 後片付け

- Ctrl+C で `web_ui_server.py` → `manager.py` の順に停止します。
- 新しいコードは `ui/` と `robot_ui/` 双方に共通モジュールを配置しているため、修正時は両方の `js/net` / `js/app` 配下が同期していることを確認してください。

