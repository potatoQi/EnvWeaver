# 项目简介

不同任务类型的数据管线往往各家独立写一套 Pipeline。想把这些 pipeline 产出的任务放在一起评测，或者拿来训练，就得给每家的 pipeline 写一套单独的适配代码。

EnvWeaver 是一个 Python 包，让不同来源的任务按同一种 Task 格式保存，这样即可用同一套接口很方便的进行 Evaluation 和管理。

## Data Pipeline

无需重写新 Pipeline，只需在已有 Pipeline 中，使用 EnvWeaver 进行轻量适配，即可按 Task 格式保存任务。

## Evaluation

对于 Task 统一格式的任务，使用 EnvWeaver 进行一键评测。支持各种主流 Harness。

## Training

训练不只需要题目，还需要知道环境怎么启动、材料放在哪里、答案怎么评分。这些信息如果散落在各个项目里，每增加一种任务，都要重新适配代码。

对于 Task 统一格式，会将这些信息打包保存。训练程序即可按 Task 各个固定好的字段按照统一流程准备环境和材料，让模型完成任务，再调用验证程序取得分数。整个过程清晰简单。
