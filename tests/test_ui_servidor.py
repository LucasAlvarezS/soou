#!/usr/bin/env python3
"""Pruebas del modo navegador de la UI.

La API local escribe notas y configuracion, asi que lo que mas importa aca es
que no sea alcanzable sin el token y que no exponga nada fuera de ui/.
Solo se invocan metodos de LECTURA contra el cerebro real.
"""

import importlib.util
import json
import re
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


TESTS = Path(__file__).resolve().parent
RAIZ = TESTS.parent

_spec = importlib.util.spec_from_file_location("soou", RAIZ / "soou.py")
soou = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(soou)


@unittest.skipUnless((RAIZ / "ui" / "index.html").exists(), "falta ui/index.html")
class ServidorUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.servidor, cls.token = soou._crear_servidor_ui(0)
        cls.hilo = threading.Thread(target=cls.servidor.serve_forever, daemon=True)
        cls.hilo.start()
        cls.base = "http://127.0.0.1:" + str(cls.servidor.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def _post(self, metodo, args=None, token=True):
        cabeceras = {"Content-Type": "application/json"}
        if token:
            cabeceras["X-Soou-Token"] = self.token
        pedido = urllib.request.Request(
            self.base + "/api/" + metodo,
            data=json.dumps({"args": args or []}).encode("utf-8"),
            headers=cabeceras, method="POST")
        try:
            with urllib.request.urlopen(pedido, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, None

    def test_la_pagina_llega_con_el_token_inyectado(self):
        html = urllib.request.urlopen(self.base + "/", timeout=30).read().decode("utf-8")
        hallado = re.search(r'window\.__SOOU_TOKEN__="([^"]+)"', html)
        self.assertIsNotNone(hallado)
        self.assertEqual(hallado.group(1), self.token)

    def test_sin_token_la_api_rechaza(self):
        codigo, _ = self._post("health", token=False)
        self.assertEqual(codigo, 403)

    def test_con_un_token_ajeno_tambien_rechaza(self):
        pedido = urllib.request.Request(
            self.base + "/api/health", data=b'{"args":[]}',
            headers={"Content-Type": "application/json", "X-Soou-Token": "otro"},
            method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(pedido, timeout=30)
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 403)

    def test_con_token_responde(self):
        codigo, datos = self._post("health")
        self.assertEqual(codigo, 200)
        self.assertIn("broken", datos)

    def test_un_metodo_privado_no_es_alcanzable(self):
        codigo, _ = self._post("_db")
        self.assertEqual(codigo, 404)

    def test_un_metodo_inexistente_da_404(self):
        codigo, _ = self._post("metodo_inventado")
        self.assertEqual(codigo, 404)

    def test_no_se_puede_salir_de_la_carpeta_ui(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/../soou.py", timeout=30)
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 404)

    def test_solo_escucha_en_loopback(self):
        self.assertEqual(self.servidor.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
