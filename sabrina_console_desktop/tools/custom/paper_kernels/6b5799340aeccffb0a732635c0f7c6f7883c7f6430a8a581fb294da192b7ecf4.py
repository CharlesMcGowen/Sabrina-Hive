def compute(rows):
    total = len(rows)
    if total == 0:
        return 0
    positive_count = sum(row[0] for row in rows)
    return positive_count / total