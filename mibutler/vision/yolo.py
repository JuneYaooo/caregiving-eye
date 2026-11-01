# -*- coding: utf-8 -*-
"""YOLOv8 ONNX 推理：检测（80 类，用到人 / 猫 / 狗）与姿态（17 个关键点）

  det = YoloOnnx('models/yolov8n.onnx');      det(frame)  -> [{'bbox','score','cls'}]
  pose = YoloOnnx('models/yolov8n-pose.onnx'); pose(frame) -> [{'bbox','score','cls','kpts'}]
根据输出形状自动区分检测模型和姿态模型。
"""
import numpy as np
import cv2
import onnxruntime as ort
from mibutler.config import cfg

PERSON, CAT, DOG = 0, 15, 16
CLASS_NAMES = {PERSON: 'person', CAT: 'cat', DOG: 'dog'}


def letterbox(img, new=640):
    h, w = img.shape[:2]
    r = min(new / h, new / w)
    nh, nw = int(h * r), int(w * r)
    top, left = (new - nh) // 2, (new - nw) // 2
    canvas = np.full((new, new, 3), 114, np.uint8)
    canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh))
    return canvas, r, left, top


class YoloOnnx:
    def __init__(self, path, providers=None):
        so = ort.SessionOptions()
        # 限制单个模型的线程数，避免多个模型与解码线程互相抢 CPU
        so.intra_op_num_threads = int(cfg('ORT_THREADS', 2))
        self.sess = ort.InferenceSession(path, so, providers or ['CPUExecutionProvider'])
        self.input = self.sess.get_inputs()[0].name
        out = self.sess.get_outputs()[0].shape
        self.is_pose = len(out) >= 2 and out[1] == 56

    def __call__(self, img, score_th=0.35):
        im, r, dx, dy = letterbox(img)
        x = im[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        pred = self.sess.run(None, {self.input: x})[0][0]  # (84|56, 8400)
        pred = pred.T
        boxes_raw = pred[:, :4]
        cls_ids = None
        kpts = None
        if self.is_pose:
            scores = pred[:, 4]
            kpts = pred[:, 5:].reshape(-1, 17, 3)
        else:
            scores = pred[:, 4:].max(axis=1)
            cls_ids = pred[:, 4:].argmax(axis=1)
            kpts = None
        keep = scores > score_th
        boxes_raw, scores = boxes_raw[keep], scores[keep]
        cls_ids = cls_ids[keep] if cls_ids is not None else None
        kpts = kpts[keep] if kpts is not None else None
        if not len(scores):
            return []
        # 模型输出 (cx, cy, w, h)；NMS 需要 (左上x, 左上y, w, h)
        xy = boxes_raw[:, :2] - boxes_raw[:, 2:] / 2
        wh = boxes_raw[:, 2:]
        boxes_px = np.concatenate([xy, wh], axis=1)
        if cls_ids is not None:
            # 按类别分别做 NMS：人抱着猫时猫的框不会被人的框吞掉
            idx = cv2.dnn.NMSBoxesBatched(boxes_px.tolist(), scores.tolist(),
                                          cls_ids.tolist(), score_th, 0.45)
        else:
            idx = cv2.dnn.NMSBoxes(boxes_px.tolist(), scores.tolist(), score_th, 0.45)
        out = []
        for i in np.array(idx).flatten():
            bx, by, w, h = boxes_px[i]
            x1 = (bx - dx) / r; y1 = (by - dy) / r
            x2 = (bx + w - dx) / r; y2 = (by + h - dy) / r
            det = {'bbox': [float(x1), float(y1), float(x2), float(y2)],
                   'score': float(scores[i]), 'cls': int(cls_ids[i]) if not self.is_pose else PERSON}
            if kpts is not None:
                k = kpts[i]
                det['kpts'] = [[float((p[0] - dx) / r), float((p[1] - dy) / r), float(p[2])] for p in k]
            out.append(det)
        return out
