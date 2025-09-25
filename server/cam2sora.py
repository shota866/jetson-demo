import cv2

import sora_sdk

#ローカルPCから取得した動画をsora経由で受信して表示する
def main():
    # Sora インスタンスの生成
    sora = sora_sdk.Sora()

    # Python SDK で映像を利用するためには create_video_source で VideoSource インスタンスを生成します
    video_source = sora.create_video_source()

    # Sora への接続設定
    conn = sora.create_connection(
        # sora-labo のシグナリング URL を設定します
        signaling_urls=["ws://sora2.uclab.jp:5000/signaling"],
        role="sendonly",
        # Sora Labo でチャネル ID とアクセストークンを作成して指定してください
        # アクセストークンはできるだけ期限を付けることをオススメします
        channel_id="sora",
        metadata={"access_token": "<access-token:str>"},
        # 音声は無効にします
        audio=False,
        video=True,
        video_source=video_source,
    )

    # デバイス ID を 0 を指定してカメラデバイスを取得します
    # ここの数字は環境によって変わってきます
    video_capture = cv2.VideoCapture(0)

    # Sora へ接続
    conn.connect()

    try:
        while True:
            # カメラデバイスからの映像を取得
            success, frame = video_capture.read()
            if not success:
                continue
            # カメラデバイスからの映像を Python SDK に渡す
            video_source.on_captured(frame)
    except KeyboardInterrupt:
        pass
    finally:
        # 切断
        conn.disconnect()
        # カメラデバイスを解放
        video_capture.release()

if __name__ == "__main__":
    main()