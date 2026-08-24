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

H06:
 - A nivel de front en donde se muestra el eestado de la version en la parte derecha de muestra u enlace hacia jenkins para ver el deploy, ese link actualmente no va a donde deberia, teniendo en cuenta la herramienta o pluggin de ocean blue debe llevarme alla para ver el detalle de los steps https://jenkins.andreslobaton.top/blue/organizations/jenkins/{APP_NAME}/detail/{APP_NAME}/{ID_JOB}/pipeline/

h07:
 - LAs anivaciones que se muestran en l parte del login se ven bien aunque en el costado derecho de algunas de las horas se ve que se corta y no logra llegar hasta el piso, lo que da ma imprecion que se corta parte de la hola. hazme preguntas para clarificarlo.

H08: Tanto en configs de secrets y configmaps, quita los 2 filtros de namespace y app, ya que estos recordando ya estan en el entorno atual entonces podria ser un poco redudantre.
H09: Como parte del trazabilidad en el backend, al generarse un error en cualquier procesos generado por una request http, debes genear un uuid por cada peticion y que sea rastreado en los diferentes logs, usando el contexto de contextvars o similar, la ides es que si se genera un error se retorne al usuari oese uuid y luego el desarrollador o persona de soporte pueda rastreasr paso por paso lo sucedido con este uuid en el api. hazme preguntas. }
H10: En el front esta url /scoops/new que es para crear un nuevo scope, en la parte de versiones permite mediante yn formulario de select mostrar las ultimas 10 versiones, tambien poder hacer busqueda conservando el liminte de 10. la idea es que sea algo simple pero versatil para el usuario.

H11:
 EN la lista de scoops, quita la columna de application y deja solo Name, ya se sabe que por seleccion global en que app se esta trabajando

H12:
- Al dar click al boton para ver los logs, en el detalle de un scope /scoops/32 no carga bien el formato de los logs.
h13:
- Actualiza la vista de edicion de un scoop, hay campos que deben sera añadidos u otros eliminados.

H14:
- Aplicar uso de sidecards para monitoreo de los scoops

H15: Comprar un dominio aparte para desplegar apps solo de productos para venta o lo que sea.
H16: COnfigurar como parametro por defecto el puerto que se expone la app el 80 pero se puede configurar al que el usuario quier.
H17: COnfigurar el desescalado a 0 en ciertos horarios segun la necesidad.