"""生成测试素材：5 张图片 + 2 段合成短视频"""

import subprocess
import os

W = 1080
H = 1920

# ── 生成 5 张测试图片 ──
for i in range(1, 6):
    # 用 FFmpeg 生成纯色图 + 文字
    color = ["0xCC4444", "0x44CC44", "0x4444CC", "0xCCCC44", "0xCC44CC"][i-1]
    text = f"Test Material {i}"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}:d=1",
        "-vf", f"drawtext=text='{text}':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2-100:fontfile=/System/Library/Fonts/PingFang.ttc,drawtext=text='AutoCat #{i}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2+50:fontfile=/System/Library/Fonts/PingFang.ttc",
        f"test_image_{i}.jpg"
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"✅ test_image_{i}.jpg")

# ── 生成 5 张不同颜色的图片 ──
for i in range(6, 11):
    color = ["0x4488AA", "0xAA4488", "0x88AA44", "0x4488CC", "0xCC8844"][i-6]
    text = f"Material {i}"
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s={W}x{H}:d=1",
        "-vf", f"drawtext=text='{text}':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2-100:fontfile=/System/Library/Fonts/PingFang.ttc,drawtext=text='素材 #{i}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=(h-text_h)/2+50:fontfile=/System/Library/Fonts/PingFang.ttc",
        f"test_image_{i}.jpg"
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"✅ test_image_{i}.jpg")

print(f"\n共生成 10 张测试图片")
print(f"目录: {os.getcwd()}")
