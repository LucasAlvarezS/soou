import unittest
from colmena.sync import intervalo_sincronizacion


class Prueba(unittest.TestCase):
    def test_intervalo(self):
        self.assertEqual(intervalo_sincronizacion(False), 600)
        self.assertEqual(intervalo_sincronizacion(True), 1800)
