from talent_training.core import TrainingNeed, summarize


if __name__ == "__main__":
    record = TrainingNeed.create("demo", "v1", "draft", "operator", {"场景": "登记培训需求并生成需求摘要"})
    print(summarize(record))

