#! /usr/bin/env python
# coding=utf-8
# ================================================================
#
#   Author      : miemie2013
#   Created date: 2020-06-10 10:20:27
#   Description : paddlepaddle_yolov4
#
# ================================================================
import os
import tempfile
import shutil
from collections import OrderedDict
import numpy as np
import paddle.fluid as fluid
import paddle.fluid.layers as P
from tools.cocotools import get_classes
from model.yolov4 import YOLOv4
from model.decode_np import Decode

import logging
FORMAT = '%(asctime)s-%(levelname)s: %(message)s'
logging.basicConfig(level=logging.INFO, format=FORMAT)
logger = logging.getLogger(__name__)




def _strip_postfix(path):
    path, ext = os.path.splitext(path)
    assert ext in ['', '.pdparams', '.pdopt', '.pdmodel'], \
            "Unknown postfix {} from weights".format(ext)
    return path

def _load_state(path):
    if os.path.exists(path + '.pdopt'):
        # XXX another hack to ignore the optimizer state
        tmp = tempfile.mkdtemp()
        dst = os.path.join(tmp, os.path.basename(os.path.normpath(path)))
        shutil.copy(path + '.pdparams', dst + '.pdparams')
        state = fluid.io.load_program_state(dst)
        shutil.rmtree(tmp)
    else:
        state = fluid.io.load_program_state(path)
    return state

def load_params(exe, prog, path, ignore_params=[]):
    """
    Load model from the given path.
    Args:
        exe (fluid.Executor): The fluid.Executor object.
        prog (fluid.Program): load weight to which Program object.
        path (string): URL string or loca model path.
        ignore_params (list): ignore variable to load when finetuning.
            It can be specified by finetune_exclude_pretrained_params
            and the usage can refer to docs/advanced_tutorials/TRANSFER_LEARNING.md
    """

    path = _strip_postfix(path)
    if not (os.path.isdir(path) or os.path.exists(path + '.pdparams')):
        raise ValueError("Model pretrain path {} does not "
                         "exists.".format(path))
    logger.debug('Loading parameters from {}...'.format(path))
    state = _load_state(path)
    fluid.io.set_program_state(prog, state)

def prune_feed_vars(feeded_var_names, target_vars, prog):
    """
    Filter out feed variables which are not in program,
    pruned feed variables are only used in post processing
    on model output, which are not used in program, such
    as im_id to identify image order, im_shape to clip bbox
    in image.
    """
    exist_var_names = []
    prog = prog.clone()
    prog = prog._prune(targets=target_vars)
    global_block = prog.global_block()
    for name in feeded_var_names:
        try:
            v = global_block.var(name)
            exist_var_names.append(str(v.name))
        except Exception:
            logger.info('save_inference_model pruned unused feed '
                        'variables {}'.format(name))
            pass
    return exist_var_names

def save_infer_model(save_dir, exe, feed_vars, test_fetches, infer_prog):
    feed_var_names = [var.name for var in feed_vars.values()]
    fetch_list = sorted(test_fetches.items(), key=lambda i: i[0])
    target_vars = [var[1] for var in fetch_list]
    feed_var_names = prune_feed_vars(feed_var_names, target_vars, infer_prog)
    logger.info("Export inference model to {}, input: {}, output: "
                "{}...".format(save_dir, feed_var_names,
                               [str(var.name) for var in target_vars]))
    fluid.io.save_inference_model(
        save_dir,
        feeded_var_names=feed_var_names,
        target_vars=target_vars,
        executor=exe,
        main_program=infer_prog,
        params_filename="__params__")


def dump_infer_config(save_dir):
    shutil.copy('tools/template_cfg.yml', '%s/infer_cfg.yml' % save_dir)


if __name__ == '__main__':
    # classes_path = 'data/voc_classes.txt'
    classes_path = 'data/coco_classes.txt'
    # 导出哪个模型
    model_path = './weights/1'

    # 推理模型输入图片大小。input_shape越大，精度会上升，但速度会下降。
    # input_shape = (320, 320)
    input_shape = (416, 416)
    # input_shape = (608, 608)

    # 推理模型保存目录
    save_dir = 'inference_model'

    # 推理时的分数阈值和nms_iou阈值。注意，该值会写死进模型，如需修改请重新导出模型。
    conf_thresh = 0.05
    nms_thresh = 0.45
    keep_top_k = 100
    nms_top_k = 100


    # 初始卷积核个数
    initial_filters = 32
    # 先验框
    anchors = np.array([
        [[12, 16], [19, 36], [40, 28]],
        [[36, 75], [76, 55], [72, 146]],
        [[142, 110], [192, 243], [459, 401]]
    ])
    # 一些预处理
    anchors = anchors.astype(np.float32)
    num_anchors = len(anchors[0])  # 每个输出层有几个先验框

    all_classes = get_classes(classes_path)
    num_classes = len(all_classes)


    startup_prog = fluid.Program()
    infer_prog = fluid.Program()
    with fluid.program_guard(infer_prog, startup_prog):
        with fluid.unique_name.guard():
            inputs = P.data(name='image', shape=[-1, 3, -1, -1], append_batch_size=False, dtype='float32')
            resize_shape = P.data(name='resize_shape', shape=[-1, 2], append_batch_size=False, dtype='int32')
            origin_shape = P.data(name='origin_shape', shape=[-1, 2], append_batch_size=False, dtype='int32')

            # 输入字典
            feed_vars = [('image', inputs), ('resize_shape', resize_shape), ('origin_shape', origin_shape)]
            feed_vars = OrderedDict(feed_vars)

            boxes, scores, classes = YOLOv4(inputs, num_classes, num_anchors, is_test=False, trainable=True, fast=True, resize_shape=resize_shape, origin_shape=origin_shape,
                                                  anchors=anchors, conf_thresh=conf_thresh, nms_thresh=nms_thresh, keep_top_k=keep_top_k, nms_top_k=nms_top_k)
            test_fetches = {'boxes': boxes, 'scores': scores, 'classes': classes, }
    infer_prog = infer_prog.clone(for_test=True)
    place = fluid.CPUPlace()
    exe = fluid.Executor(place)
    exe.run(startup_prog)

    load_params(exe, infer_prog, model_path)

    save_infer_model(save_dir, exe, feed_vars, test_fetches, infer_prog)
    dump_infer_config(save_dir)

