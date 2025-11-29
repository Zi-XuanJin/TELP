import os
import pandas as pd


def calculate_average_metrics(root_dir="."):
    """
    读取指定目录下所有文件夹中的Excel文件，计算每个文件的平均指标，
    并将结果汇总到一个新的Excel文件中。

    Args:
        root_dir (str): 要搜索的根目录，默认为当前目录。
    """
    # 存储所有文件的平均指标
    all_metrics = []

    # 遍历根目录下的所有文件夹
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.endswith('.xlsx') or filename.endswith('.xls'):
                file_path = os.path.join(dirpath, filename)
                print(f"正在处理文件: {file_path}")

                try:
                    # 读取Excel文件
                    df = pd.read_excel(file_path)

                    # 计算每个指标的平均值
                    average_row = df.mean(numeric_only=True)

                    # 将文件名作为第一列
                    average_row['file_name'] = filename

                    # 将结果添加到列表中
                    all_metrics.append(average_row)

                except Exception as e:
                    print(f"处理文件 {file_path} 时出错: {e}")

    if not all_metrics:
        print("未找到任何Excel文件进行处理。")
        return

    # 将列表转换为DataFrame
    results_df = pd.DataFrame(all_metrics)

    # 将'file_name'列移到第一列
    cols = ['file_name'] + [col for col in results_df.columns if col != 'file_name']
    results_df = results_df[cols]

    # 将结果保存到新的Excel文件
    output_path = os.path.join(root_dir, 'overall_metrics.xlsx')
    results_df.to_excel(output_path, index=False)

    print(f"\n所有文件的平均指标已成功保存到: {output_path}")


# 调用函数
if __name__ == "__main__":
    calculate_average_metrics()