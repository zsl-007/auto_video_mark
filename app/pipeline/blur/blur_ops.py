import cv2


def gaussian_inplace(frame, x1, y1, x2, y2, strength_divisor=8.0):
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return
    kw = max(3, int((x2 - x1) / strength_divisor) | 1)
    kh = max(3, int((y2 - y1) / strength_divisor) | 1)
    frame[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (kw, kh), 0)


def pixelate_inplace(frame, x1, y1, x2, y2, blocks: int = 12):
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return
    h, w = roi.shape[:2]
    fx = max(1, w // blocks)
    fy = max(1, h // blocks)
    small = cv2.resize(roi, (max(1, w // fx), max(1, h // fy)), interpolation=cv2.INTER_LINEAR)
    frame[y1:y2, x1:x2] = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)

def face_blur_inplace(
    frame,
    x1,
    y1,
    x2,
    y2,
    method="pixelate",
    strength=7,
):
    """
    人脸专用模糊。

    strength: 1~10，数值越大，模糊越强。
    """
    strength = max(1, min(10, int(strength)))

    if method == "gaussian":
        # strength 越大，divisor 越小，卷积核越大
        strength_divisor = max(1.5, 18.0 / strength)
        gaussian_inplace(
            frame, x1, y1, x2, y2,
            strength_divisor=strength_divisor,
        )
    else:
        # strength 越大，像素块越少，马赛克越明显
        blocks = max(2, 14 - strength)
        pixelate_inplace(
            frame, x1, y1, x2, y2,
            blocks=blocks,
        )