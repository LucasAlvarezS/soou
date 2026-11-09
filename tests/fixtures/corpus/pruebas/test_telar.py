import unittest
from telar.turnos import turno_valido


class Prueba(unittest.TestCase):
    def test_duracion(self):
        self.assertTrue(turno_valido(8))
        self.assertTrue(turno_valido(9))
        self.assertFalse(turno_valido(9.5))
        self.assertFalse(turno_valido(10))
