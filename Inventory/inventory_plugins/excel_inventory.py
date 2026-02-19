#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
excel_inventory.py  (Inventory Plugin para Ansible)

✅ Objetivo
- Generar inventario dinámico desde un Excel.
- Soporta 2 fuentes:
  1) local:     lee un archivo .xlsx del filesystem
  2) sharepoint: descarga el .xlsx desde SharePoint Online a un archivo temporal y lo procesa

✅ Encabezados esperados en tu Excel (tal cual):
  Host | Grupo | IP | Usuario | Descripción | Python Interpreter

✅ Qué construye en Ansible
- inventory_hostname  = columna "Host" (en tu Excel hoy pones IP; funciona, aunque ideal sería hostname/FQDN)
- grupos              = columna "Grupo" (tú la llenas manual)
- ansible_host         = columna "IP"
- ansible_user         = columna "Usuario"
- description          = columna "Descripción"
- ansible_python_interpreter = columna "Python Interpreter" (si viene vacía, no la asigna)

📌 Ubicación recomendada (según tu estructura):
inventory/inventory_plugins/excel_inventory.py

📌 ansible.cfg (en la raíz del repo, o donde ejecutes Ansible):
[defaults]
inventory_plugins = ./inventory/inventory_plugins

📌 Nombre del archivo YAML de inventario:
Debe terminar en: excel_inventory.yml  o  excel_inventory.yaml
Ej: inventory/linux_excel_inventory.excel_inventory.yml
"""

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import sys
import tempfile
from typing import Any, Dict, Union, Optional

from ansible.plugins.inventory import BaseInventoryPlugin, Constructable, Cacheable
from ansible.errors import AnsibleError, AnsibleParserError
from ansible.config.manager import ensure_type
from ansible.utils.display import Display

display = Display()

# -----------------------------
# Dependencias externas
# -----------------------------
try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

try:
    from office365.runtime.auth.client_credential import ClientCredential
    from office365.sharepoint.client_context import ClientContext
    HAS_OFFICE365 = True
except ImportError:
    HAS_OFFICE365 = False


DOCUMENTATION = r'''
name: excel_inventory
short_description: Inventario dinámico desde Excel (local o SharePoint)
description:
  - Lee un Excel y construye inventario Ansible (hosts, variables y grupos).
  - Si source_type=sharepoint, descarga el Excel desde SharePoint Online.

options:
  plugin:
    description: Identificador del plugin
    required: true
    choices: ['excel_inventory']

  source_type:
    description: Origen del Excel
    required: true
    choices: ['local', 'sharepoint']

  sheet_name:
    description: Nombre de hoja a leer. Si se omite, usa la primera hoja.
    required: false
    type: str

  local_config:
    description: Configuración cuando source_type=local
    required: false
    type: dict
    suboptions:
      file_path:
        description: Ruta del Excel local (.xlsx/.xls)
        required: true
        type: path

  sharepoint_config:
    description: Configuración cuando source_type=sharepoint
    required: false
    type: dict
    suboptions:
      site_url:
        required: true
        type: str
      folder_path:
        required: true
        type: str
      filename:
        required: true
        type: str
      tenant_id:
        required: true
        type: str
      client_id:
        required: true
        type: str
      client_secret:
        required: true
        type: str

  hostname_column:
    description: Columna del Excel usada como inventory_hostname
    required: false
    type: str
    default: Host

  compose:
    description: Mapeo variable_Ansible -> columna_Excel
    required: false
    type: dict
    default: {}

  keyed_groups:
    description: Crear grupos a partir de valores únicos de una columna (ej: Grupo)
    required: false
    type: list
    default: []
    suboptions:
      key:
        required: true
        type: str
      prefix:
        required: false
        type: str
        default: ""
      separator:
        required: false
        type: str
        default: "_"
'''

EXAMPLES = r'''
# Ejemplo SharePoint con tus columnas (Host, Grupo, IP, Usuario, Descripción, Python Interpreter)
---
plugin: excel_inventory
source_type: sharepoint
sheet_name: "Servidores Linux"

sharepoint_config:
  site_url: "https://tuempresa.sharepoint.com/sites/AreaInfraestructura"
  folder_path: "/sites/AreaInfraestructura/Documentos compartidos/General/Inventario Infraestructura"
  filename: "Sincronizacion_Parcheado_SO.xlsx"
  tenant_id: "{{ lookup('env', 'TENANT_ID') }}"
  client_id: "{{ lookup('env', 'CLIENT_ID') }}"
  client_secret: "{{ lookup('env', 'CLIENT_SECRET') }}"

hostname_column: "Host"

compose:
  ansible_host: "IP"
  ansible_user: "Usuario"
  description: "Descripción"
  ansible_python_interpreter: "Python Interpreter"

keyed_groups:
  - key: "Grupo"
'''


class InventoryModule(BaseInventoryPlugin, Constructable, Cacheable):
    """
    Plugin de inventario: excel_inventory
    """
    NAME = 'excel_inventory'

    # -----------------------------
    # Inicialización básica
    # -----------------------------
    def __init__(self) -> None:
        if sys.version_info < (3, 6):
            raise AnsibleError("Se requiere Python 3.6+ para este plugin.")
        super().__init__()

    # -----------------------------
    # Verifica que el archivo YAML sea para este plugin
    # (Esto hace que Ansible lo intente cargar solo para estos nombres)
    # -----------------------------
    def verify_file(self, path: str) -> bool:
        if super().verify_file(path):
            return path.endswith(('excel_inventory.yml', 'excel_inventory.yaml'))
        return False

    # -----------------------------
    # Entrada principal del plugin
    # Ansible llama parse() cuando ejecutas:
    #   ansible-inventory -i archivo.excel_inventory.yml --list
    # -----------------------------
    def parse(self, inventory, loader, path: str, cache: bool = True) -> None:
        super().parse(inventory, loader, path, cache)

        self._check_requirements()

        # Lee el YAML (config del inventario)
        config = self._read_config_data(path)

        # Campos principales
        self.source_type = self._get_str(config, 'source_type', required=True)
        self.sheet_name = self._get_str(config, 'sheet_name', required=False, default="")
        self.hostname_column = self._get_str(config, 'hostname_column', required=False, default="Host")

        # Bloques de config
        self.local_config = ensure_type(config.get("local_config", {}), "dict")
        self.sharepoint_config = ensure_type(config.get("sharepoint_config", {}), "dict")

        # Mapeos / grupos
        self.compose = ensure_type(config.get("compose", {}), "dict")
        self.keyed_groups = ensure_type(config.get("keyed_groups", []), "list")

        # Validación básica
        if self.source_type not in ("local", "sharepoint"):
            raise AnsibleParserError("source_type debe ser 'local' o 'sharepoint'.")

        # Ejecuta según fuente
        if self.source_type == "local":
            self._process_local_source()
        else:
            self._process_sharepoint_source()

    # -----------------------------
    # Validación de dependencias
    # -----------------------------
    def _check_requirements(self) -> None:
        if not HAS_PANDAS:
            raise AnsibleError(
                "Falta 'pandas'. Instala: pip3 install pandas openpyxl"
            )
        # Office365 solo se exige si source_type=sharepoint (validamos después)

    # -----------------------------
    # Helpers para leer strings del YAML
    # -----------------------------
    def _get_str(self, config: Dict[str, Any], key: str, required: bool = False, default: str = "") -> str:
        val = config.get(key, None)
        if val is None:
            if required:
                raise AnsibleParserError(f"Falta el parámetro requerido: {key}")
            return default
        return ensure_type(val, "str").strip()

    # -----------------------------
    # Procesamiento local
    # -----------------------------
    def _process_local_source(self) -> None:
        file_path = (self.local_config or {}).get("file_path")
        if not file_path:
            raise AnsibleParserError("source_type=local requiere local_config.file_path")

        if not os.path.exists(file_path):
            raise AnsibleParserError(f"No existe el archivo Excel local: {file_path}")

        df = self._read_excel(file_path)
        self._populate_inventory(df)

    # -----------------------------
    # Procesamiento SharePoint
    # -----------------------------
    def _process_sharepoint_source(self) -> None:
        if not HAS_OFFICE365:
            raise AnsibleError(
                "Falta Office365-REST-Python-Client. Instala: pip3 install Office365-REST-Python-Client"
            )

        cfg = self.sharepoint_config or {}
        required = ["site_url", "folder_path", "filename", "tenant_id", "client_id", "client_secret"]
        missing = [k for k in required if not cfg.get(k)]
        if missing:
            raise AnsibleParserError(f"Faltan campos en sharepoint_config: {', '.join(missing)}")

        tmp_path: Optional[str] = None
        try:
            tmp_path = self._download_from_sharepoint(cfg)
            df = self._read_excel(tmp_path)
            self._populate_inventory(df)
        finally:
            # Limpieza del archivo temporal
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def _download_from_sharepoint(self, cfg: Dict[str, Any]) -> str:
        """
        Descarga el Excel desde SharePoint a un archivo temporal.
        """
        site_url = cfg["site_url"]
        folder_path = cfg["folder_path"]
        filename = cfg["filename"]

        client_id = cfg["client_id"]
        client_secret = cfg["client_secret"]
        # tenant_id se conserva porque lo pides por env var y es estándar en Azure
        # (en este flujo específico, ClientCredential usa client_id/secret)
        _tenant_id = cfg["tenant_id"]

        # Ruta relativa dentro del site
        file_url = f"{folder_path}/{filename}"

        display.vvv(f"SharePoint site_url: {site_url}")
        display.vvv(f"SharePoint file_url: {file_url}")

        # Auth (client credentials)
        credentials = ClientCredential(client_id, client_secret)
        ctx = ClientContext(site_url).with_credentials(credentials)

        # Temporal
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx", prefix="ans_inv_")
        temp_path = temp_file.name
        temp_file.close()

        display.vvv(f"Descargando Excel a temporal: {temp_path}")

        try:
            with open(temp_path, "wb") as local_file:
                sp_file = ctx.web.get_file_by_server_relative_url(file_url)
                sp_file.download(local_file).execute_query()
        except Exception as e:
            # Si falla, borra el temporal y levanta error
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise AnsibleError(f"Error descargando archivo desde SharePoint: {e}")

        return temp_path

    # -----------------------------
    # Lectura del Excel con pandas
    # -----------------------------
    def _read_excel(self, file_path: str):
        """
        Lee el Excel (primera hoja o sheet_name si se definió).
        Valida que exista la columna hostname_column.
        Limpia filas vacías en hostname_column.
        """
        sheet: Union[str, int] = self.sheet_name if self.sheet_name else 0

        try:
            df = pd.read_excel(file_path, sheet_name=sheet, engine="openpyxl")
        except Exception as e:
            raise AnsibleError(f"Error leyendo Excel: {e}")

        # Valida columna de hostname
        if self.hostname_column not in df.columns:
            raise AnsibleParserError(
                f"No existe la columna hostname_column='{self.hostname_column}'. "
                f"Columnas disponibles: {', '.join(df.columns.tolist())}"
            )

        # Quita filas donde Host esté vacío
        df = df.dropna(subset=[self.hostname_column])
        df = df[df[self.hostname_column].astype(str).str.strip() != ""]

        return df

    # -----------------------------
    # Construcción del inventario
    # -----------------------------
    def _populate_inventory(self, df) -> None:
        """
        Crea hosts + variables + grupos.

        - Host se toma de hostname_column (por defecto: "Host")
        - Variables se asignan según compose
        - Grupos se crean con keyed_groups (por defecto úsalo con key: "Grupo")
        """
        # 1) Crear hosts y asignar variables
        for _, row in df.iterrows():
            hostname = str(row[self.hostname_column]).strip()
            if not hostname:
                continue

            # Agrega host a inventario
            self.inventory.add_host(hostname)

            # Asigna variables desde compose
            # Ej:
            # compose:
            #   ansible_host: "IP"
            #   ansible_user: "Usuario"
            #   description: "Descripción"
            #   ansible_python_interpreter: "Python Interpreter"
            for ansible_var, excel_col in (self.compose or {}).items():
                if excel_col not in df.columns:
                    # Si no existe la columna, se ignora (y avisa en verbose)
                    display.vvv(f"compose: columna '{excel_col}' no existe (var '{ansible_var}')")
                    continue

                val = row.get(excel_col)

                # Evitar NaN o vacíos
                if pd.isna(val):
                    continue
                if isinstance(val, str) and val.strip() == "":
                    continue

                # Set variable al host
                self.inventory.set_variable(hostname, ansible_var, val)

        # 2) Crear grupos por columna (keyed_groups)
        # Recomendado para tu caso:
        # keyed_groups:
        #   - key: "Grupo"
        for grp in (self.keyed_groups or []):
            if not isinstance(grp, dict) or "key" not in grp:
                continue

            key_col = grp["key"]
            prefix = grp.get("prefix", "")
            separator = grp.get("separator", "_")

            if key_col not in df.columns:
                display.warning(f"keyed_groups: columna '{key_col}' no existe en el Excel")
                continue

            for group_value, gdf in df.groupby(key_col):
                if pd.isna(group_value) or str(group_value).strip() == "":
                    continue

                # Normaliza nombre: "BIA" -> "bia", "Mi Grupo" -> "mi_grupo"
                base = str(group_value).strip().lower().replace(" ", "_").replace("-", "_")
                group_name = f"{prefix}{separator}{base}" if prefix else base

                self.inventory.add_group(group_name)

                # Añade hosts al grupo
                for _, r in gdf.iterrows():
                    hn = str(r[self.hostname_column]).strip()
                    if hn:
                        self.inventory.add_child(group_name, hn)
