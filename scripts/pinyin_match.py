#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
拼音键与拼音索引
================

## 解决什么

用户把「卡维地洛」打成「卡位低落」——**读起来一样，写出来三个字都不一样**。
现有的匹配是**字形**匹配（子串 / 前缀），对这种输入完全无能为力：

    卡维地洛 → ka wei di luo
    卡位低落 → ka wei di luo      ← 拼音完全相同

## 为什么不用编辑距离

「卡位低落」与「卡维地洛」字形差 3 个字（共 5 个字）。用编辑距离去兜意味着
"5 字药名容忍 3 字差异"——那已经没有任何区分度，会配出一堆不相关的药。
**同音错别字在拼音上是精确相等的**，用拼音键既准又不会放宽字面匹配的标准。

## 两个刻意的限制

1. **只做整词或整词前缀匹配**，不在拼音串中间找子串——否则 `luo`（洛/落/络）
   会命中所有以 luo 结尾的药。
2. **命中多个时返回候选列表，不替用户挑一个**。同一个拼音可能对应好几个不同的药
   （多音字 `地`：dì / de；`差`：chā / chà），选错药在这个系统里是有安全后果的。

## 已知局限

`lazy_pinyin` 取的是**默认读音**，「地」在「卡维地洛」里读 dì 没问题，
但像「干」在「干姜」里是 gān、在「干部」里是 gàn——本模块不会做上下文消歧。
实测这只会导致**漏匹配**（拼音对不上），不会导致**错匹配**（配出一个读音不同的药），
方向上是安全的。
"""

from __future__ import annotations

from pypinyin import Style, lazy_pinyin

#: 拼音键至少要这么长才参与匹配。低于 4 个字符（≈ 1 个音节）的拼音
#: 覆盖太多药名，匹配没有意义。
MIN_KEY_LEN = 4


def pinyin_key(text: str) -> str:
    """把一个名字转成「无音调、音节用空格分隔」的拼音键。

    非汉字的字符原样保留（小写），这样「维生素C」→ ``wei sheng su c``，
    「18AA」这种编码也不会被丢掉。
    """
    if not text:
        return ""
    syllables = lazy_pinyin(text, style=Style.NORMAL, errors=lambda chars: list(chars))
    return " ".join(s.lower() for s in syllables if s).strip()


class PinyinIndex:
    """名字 → 拼音键的倒排索引（整词 + 整词前缀）。"""

    def __init__(self, names: list[str]):
        self._exact: dict[str, list[str]] = {}
        self._prefix: dict[str, list[str]] = {}
        for name in names:
            key = pinyin_key(name)
            if len(key.replace(" ", "")) < MIN_KEY_LEN:
                continue
            self._exact.setdefault(key, []).append(name)
            # 前缀表：只登记"到音节边界"的前缀，避免在音节中间切断
            cut = 0
            for i, ch in enumerate(key):
                if ch == " ":
                    cut = i
                    if cut >= MIN_KEY_LEN:
                        self._prefix.setdefault(key[:cut], []).append(name)
        self.size = len(self._exact)

    def match(self, query: str, limit: int = 8) -> tuple[list[str], str]:
        """按读音匹配。返回 ``(名字列表, 匹配方式)``，方式 ∈ {'pinyin', 'pinyin-prefix', ''}。"""
        key = pinyin_key(query)
        if len(key.replace(" ", "")) < MIN_KEY_LEN:
            return [], ""

        # ① 整词：同音错别字走这一条（卡位低落 → ka wei di luo → 卡维地洛）
        hit = self._exact.get(key)
        if hit:
            return hit[:limit], "pinyin"

        # ② 整词前缀：同音 + 少说几个字（卡位 → ka wei → 卡维地洛）
        best: list[str] = []
        for cut in sorted(self._prefix, key=len, reverse=True):
            if key.startswith(cut):
                best = self._prefix[cut]
                break
        if best:
            return best[:limit], "pinyin-prefix"
        return [], ""
