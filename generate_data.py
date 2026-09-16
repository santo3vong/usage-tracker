import json, os, sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import server

OUTPUT_FILE = os.path.join(BASE_DIR, 'data.js')

def main():
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    data = server.analyze_all_conversations()

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write('const USAGE_DATA = ')
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write(';')

    summary = data.get('summary', {})
    print(f"Generated data for {summary.get('total_conversations', 0)} conversations")
    print(f"Total input tokens (est): {summary.get('total_input_tokens', 0):,}")
    print(f"Total output tokens (est): {summary.get('total_output_tokens', 0):,}")
    print(f"Total estimated cost USD: ${summary.get('total_estimated_cost_usd', 0):.4f}")
    print(f"Output written to: {OUTPUT_FILE}")

if __name__ == '__main__':
    main()
