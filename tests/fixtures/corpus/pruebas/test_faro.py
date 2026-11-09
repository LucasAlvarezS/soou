import unittest
from faro.alertas import alerta_bateria


class Prueba(unittest.TestCase):
    def test_umbral(self):
        self.assertTrue(alerta_bateria(14.9))
        self.assertTrue(alerta_bateria(0))
        self.assertFalse(alerta_bateria(15))
        self.assertFalse(alerta_bateria(18))
        self.assertFalse(alerta_bateria(80))
