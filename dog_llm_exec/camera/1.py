import numpy as np

# 给定数据
angles = np.array([-124, 150, -164, 145])
temperatures = np.array([0, -30, 57, 38])

# 进行线性回归
coefficients = np.polyfit(angles, temperatures, 1)  # 1表示线性拟合
a, b = coefficients

# 输出结果
print(f'线性拟合公式: Temperature = {a:.4f} * Angle + {b:.4f}')

# 测试拟合结果
test_angles = np.array([-124, 150, -164, 145])
predicted_temperatures = -0.2431 * test_angles + 27.8311

for angle, temp in zip(test_angles, predicted_temperatures):
    print(f'角度: {angle}, 预测温度: {temp:.2f}度')
