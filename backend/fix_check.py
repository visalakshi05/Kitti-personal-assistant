data = open(r'C:\Users\Visalakshi\Documents\vscode\project\personal_assistant\backend\main.py', 'r', encoding='utf-8').read()
lines = data.split('\n')

# Find all lines with " in them (potential string issues)
print("All lines containing double quotes in the first 100 lines:")
for i in range(min(100, len(lines))):
    line = lines[i]
    if '"' in line and i > 40:
        # Count quotes
        cnt = line.count('"')
        print(f"Line {i+1} ({cnt} quotes): {repr(line)}")