def compute(rows):
    return sum(row[0] for row in rows) / len(rows) if rows else 0