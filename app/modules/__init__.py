"""Modulos de dominio.

Cada modulo es autocontenido (model, schema, service, controller) y se apoya en
`app.core` para la infraestructura compartida.

  scoops  -> infraestructura de una aplicacion: catalogo de componentes y su
             despliegue en el cluster. Es el servicio principal del proyecto.
  cluster -> acceso directo a recursos nativos de Kubernetes.
  audits  -> trazabilidad de las mutaciones.
"""
