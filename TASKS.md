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
- Al consultar estado del deployment, validar que se tome el id correcto del deploy (no es el mismo que el id de la aplicación). **RESUELTO en este turno**: get_build_status ahora detecta URLs de queue (/queue/item/<id>/) y las resuelve de 3 formas: (a) queue API 200 + executable.url -> sigue el executable, (b) queue API 200 sin executable -> pending, (c) queue API 404 (item consumido) -> fallback a job.lastBuild. Tests en TestGetBuildStatusQueue (4 nuevos tests, todos pasan).

H05:
 - EN el front al momento de crear un nuevo secrets asociado a una app, recuerda que en el estado global ya tienes la app seleccionada por lo que el form es inncesario del application, y el de namespace cada app crea un namespaces internamente por lo que tambien es innecesario ese campo,
 - Permite unicamente desde el front poder importar un archivo .env y autollenar ciertas campos de formulario, hazme preguntas
 - Igual en configmaps,
 - Recuerda que estos 2 recursos de kubernetes se deben crear tal cual con los servicios respectivos.
 - Importante cuando se quiera editar, no debe mostrar valores reales de las credenciales, es decir ocultas, con un boton a la derecha para verlas, solo en los secrets.