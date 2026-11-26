def compute(rows):
    total_error = 0
    for row in rows:
        prediction, target = row
        error = abs(prediction - target)
        total_error += error
    return total_error / len(rows)