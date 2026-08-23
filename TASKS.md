HOY: 1
- LA UI para seleccionar una app y una workspace debe ser diferente en colores.
- Cuando clickeosobre la app arriba deberia permitrme cambiar de app
- Al eliminar un aapp debe eliminarla de verdad, guardar log de toda la config para trazavilidad.
- AL crear un scoop el registry ya lo tiene por debajo no debe solicitarlo
- Al crear secrets debe permitir subir .env

HO2 2:
 - Sitema de logs mas eficiente, añadir debug con id de request global. por cada paso debe dejar un los de DEBUG de confirmacion, los errores debe dejarlos con traceback en todo lado.
 -

H03:
 - En el proceso de deploy aplicar difernetes estrategias y tambien que tome la nueva version asi como algunc config que se haya creado adicional o secrets.

H04:
- En el proceso de crear una nueva app siempre debe crear el repositorio, quitar aviso: Crear repo vacio en GitHub al guardar (requiere PAT configurado)
- Cuando se elimina una app, refrescarla del entorno actual segun el workspace
- Aplicar la seguridad entre github y mi app
- Apliar seguridad entre jenkins y mi app
- Aplicar seguridad en jenkins para no enviar credenciales en peticion http sino que esten en jenkins, credenciales de docker hub y github
- Al cosultar estado del deployment, debe validar para que tome el id correco del deploy, por que no es igual sl id de la applicacion. atualmente esta mal.