---
name: faro-protocolo
area: code
type: decision
sources: []
pinned: false
updated: 2026-10-01
supersedes: faro-protocolo-v1
recorded_at: 2026-05-20
verification: revisado
---

# Protocolo de envio

Las balizas publican sus lecturas por MQTT sobre LoRaWAN, en el topico `faro/<id>/lecturas`, cada 2 minutos. [[proj-faro]]
