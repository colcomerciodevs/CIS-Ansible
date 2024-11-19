#!/usr/bin/env python3
import sys
import json
import pandas as pd

# Ruta del archivo Excel
excel_file = 'inventory_data.xlsx'

def parse_excel(file):
    # Leer el archivo Excel
    df = pd.read_excel(file)
    inventory = {"_meta": {"hostvars": {}}}

    for _, row in df.iterrows():
        host = row['host']
        group = row['group']
        vars = {"ansible_host": row['ip'], "ansible_user": row['user']}

        if group not in inventory:
            inventory[group] = {"hosts": [], "vars": {}}
        inventory[group]["hosts"].append(host)
        inventory["_meta"]["hostvars"][host] = vars

    return inventory

def main():
    if len(sys.argv) == 2 and sys.argv[1] == '--list':
        inventory = parse_excel(excel_file)
        print(json.dumps(inventory, indent=2))
    elif len(sys.argv) == 2 and sys.argv[1] == '--host':
        # Devuelve las variables del host (si es necesario)
        print(json.dumps({}))
    else:
        print("Usage: --list | --host <hostname>")
        sys.exit(1)

if __name__ == '__main__':
    main()
