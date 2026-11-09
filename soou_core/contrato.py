"""Contrato interno entre soou y cualquier agente: lo que se pide (TaskRequest) y
lo que vuelve (TaskResult), con estados y codigos de error estables.

Capa base: no importa nada de soou. Un adaptador nuevo (CLI, HTTP, ACP...) solo
tiene que traducir su protocolo a estas estructuras; el ejecutor, el router y el
cerebro no cambian.

`verificado` NO es un booleano que alguien escribe: se deriva de las
verificaciones que exige una politica con nombre. Asi no puede contradecirlas."""

from dataclasses import dataclass, field

ESTADOS = ("completado", "sin_cambios", "fallo", "timeout", "rechazado", "cancelado")

# Codigos estables: los consumen las trazas, el routing aprendido y la UI.
E_PERMISO = "E_PERMISO"              # el agente no puede cumplir el modo pedido
E_TIMEOUT = "E_TIMEOUT"
E_NO_PUDO = "E_NO_PUDO"              # termino "bien" pero declara que no pudo
E_LANZAMIENTO = "E_LANZAMIENTO"      # no se pudo lanzar el proceso
E_SALIDA_INVALIDA = "E_SALIDA_INVALIDA"
E_NO_DISPONIBLE = "E_NO_DISPONIBLE"
E_PRESUPUESTO = "E_PRESUPUESTO"      # limite diario de invocaciones
E_SIN_CAMBIOS = "E_SIN_CAMBIOS"      # escritura pedida y nada cambio
E_PROCESO = "E_PROCESO"              # el agente termino con error

# Estados de una verificacion. "no_ejecutado" NUNCA cuenta como aprobado.
OK, FALLO, NO_EJECUTADO, NO_APLICA = "ok", "fallo", "no_ejecutado", "no_aplica"

# Politicas: que verificaciones tienen que estar presentes y aprobadas. Todas
# incluyen el minimo; la complejidad suma controles, nunca los quita.
MINIMO = ("proceso", "respuesta", "cambios", "no_pudo", "permisos")
POLITICAS = {
    "minimo": MINIMO,
    "validadores": MINIMO + ("validadores",),
    "revision": MINIMO + ("validadores", "revision"),
}


@dataclass
class TaskRequest:
    tarea: str
    contexto: str = ""
    workspace: str | None = None
    modo: str = "leer"                   # "leer" | "escribir"
    exige_cambios: bool | None = None    # None = lo que diga el modo
    modelo: str | None = None
    reasoning: str | None = None
    timeout: int = 1200
    run_id: str = ""
    paso: str = ""
    nivel: str = ""
    operacion: str = ""                  # etiqueta de la traza (p.ej. "distill")
    conservar_parcial: bool = False      # trabajo aditivo: sin checkpoint ni vuelta atras

    @property
    def escritura(self) -> bool:
        return self.modo == "escribir"

    @property
    def requiere_cambios(self) -> bool:
        return self.escritura if self.exige_cambios is None else self.exige_cambios

    def prompt(self) -> str:
        if not self.contexto:
            return self.tarea
        return f"Contexto del cerebro (soou):\n{self.contexto}\n\nTarea: {self.tarea}"


@dataclass
class Verificacion:
    nombre: str
    estado: str                          # ok | fallo | no_ejecutado | no_aplica
    detalle: str = ""


@dataclass
class TaskResult:
    estado: str
    agente: str = ""
    respuesta: str = ""
    codigo_error: str | None = None
    artefactos: list = field(default_factory=list)
    uso_reportado: dict | None = None    # None = el agente no lo informo; nunca se inventa
    duracion_ms: int = 0
    eventos: list = field(default_factory=list)
    verificaciones: list = field(default_factory=list)
    politica: str = "minimo"
    intento: int = 0

    @property
    def proceso_ok(self) -> bool:
        return self.estado in ("completado", "sin_cambios")

    def verificacion(self, nombre):
        return next((v for v in self.verificaciones if v.nombre == nombre), None)

    def cumple(self, politica: str | None = None) -> bool:
        """Todas las verificaciones exigidas estan presentes y aprobadas
        (`no_aplica` cuenta como aprobada; `no_ejecutado`, no)."""
        for nombre in POLITICAS[politica or self.politica]:
            v = self.verificacion(nombre)
            if v is None or v.estado not in (OK, NO_APLICA):
                return False
        return True

    @property
    def verificado(self) -> bool:
        return self.cumple()

    @property
    def motivo(self) -> str:
        """La primera verificacion que fallo, legible; vacio si cumple."""
        for nombre in POLITICAS[self.politica]:
            v = self.verificacion(nombre)
            if v is None:
                return f"{nombre}: no se comprobo"
            if v.estado not in (OK, NO_APLICA):
                return v.detalle or f"{nombre}: {v.estado}"
        return ""

    def resumen_verificacion(self) -> str:
        return ", ".join(f"{v.nombre}={v.estado}" for v in self.verificaciones)


def desde_legado(d: dict, agente: str = "", duracion_ms: int = 0) -> TaskResult:
    """Traduce la salida historica de un adaptador ({ok, output, [estado,
    codigo_error]}) al contrato. Lo que el adaptador no informa queda vacio."""
    d = d or {}
    estado = d.get("estado")
    if estado not in ESTADOS:
        estado = "completado" if d.get("ok") else "fallo"
    codigo = d.get("codigo_error") or (None if estado == "completado" else E_PROCESO)
    return TaskResult(estado=estado, agente=agente, respuesta=d.get("output") or "",
                      codigo_error=codigo, uso_reportado=d.get("uso"),
                      eventos=list(d.get("eventos") or []), duracion_ms=duracion_ms)
