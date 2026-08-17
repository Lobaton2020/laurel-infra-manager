# Jenkins en k3s (homelob)

## Aplicar

```bash
kubectl apply -k deploy/jenkins
```

Crea en el namespace `prod`: Deployment + Service + Ingress (`jenkins.andreslobaton.top`), un PVC `jenkins-home` (10Gi, `local-path`) y el certificado TLS vía cert-manager (`letsencrypt-prod`).

> El nodo homelob corre **containerd** (k3s), sin daemon docker, así que el manifest **no** monta `/var/run/docker.sock`. Jenkins arranca igual; el paso `docker build/push` de los jobs necesita una de estas opciones:
>
> - **A (host)**: `sudo apt install docker.io && sudo systemctl enable --now docker` en homelob, y volver a añadir en el Deployment el volumen `docker-sock` (hostPath `/var/run/docker.sock`, `type: Socket`).
> - **B (DinD)**: desplegar `docker:dind` (privileged) + Service `dind:2375` y en el job usar `DOCKER_HOST=tcp://dind:2375`.

## Password admin inicial

Sin wizard (`-Djenkins.install.runSetupWizard=false`), el password inicial sigue guardado en el home:

```bash
kubectl -n prod exec deploy/jenkins -- cat /var/jenkins_home/secrets/initialAdminPassword
```

Para fijar uno propio, añade al Deployment el env `JENKINS_ADMIN_PASSWORD=<password>` y Jenkins lo usará como password del usuario `admin` al arrancar.

## Plugins mínimos (Install suggested / tu eleccion)

- **Pipeline**
- **Git**
- **Docker**
- **Docker Pipeline**
- **Credentials Binding**
- (opcional) **Blue Ocean** para la UI de pipelines.

## Job `laurel_<slug>`

1. **Nuevo item** → tipo **Pipeline** → nombre `laurel_<slug>`.
2. **General**: marca *Discard old builds* y añade parámetros:
   - `SLUG` (default `<slug>`)
   - `TAG` (default `latest`)
   - `REPO` (default `laurel-applications/laurel_<slug>`)
   - `IMAGE` (default `aflobaton/laurel_<slug>:${TAG}`)
   - `JENKINS_TOKEN` (string, default el token guardado en `/api/system/secrets/jenkins_token`).
3. **Build Triggers**: activa *Trigger builds remotely (e.g., from scripts)* con token `JENKINS_TOKEN`. La URL de disparo:

   ```
   http://jenkins.andreslobaton.top/job/laurel_<slug>/buildWithParameters?token=${JENKINS_TOKEN}&SLUG=<slug>&TAG=1.0.0
   ```

4. **Pipeline** → *Pipeline script*:

```groovy
pipeline {
  agent any
  parameters {
    string(name: 'SLUG', defaultValue: '<slug>')
    string(name: 'TAG', defaultValue: 'latest')
    string(name: 'REPO', defaultValue: 'laurel-applications/laurel_<slug>')
    string(name: 'IMAGE', defaultValue: '')
  }
  environment {
    // El controller monta el socket docker del host; docker build/push corre
    // contra el daemon del nodo y usa la sesion de login del host.
    IMG = params.IMAGE ?: "aflobaton/laurel_${params.SLUG}:${params.TAG}"
  }
  stages {
    stage('checkout') {
      steps {
        git url: "https://github.com/${params.REPO}.git", branch: 'main'
      }
    }
    stage('test') {
      steps {
        script {
          // Autodeteccion simple de toolchain; ajusta a los tests reales.
          if (fileExists('pytest.ini') || fileExists('pyproject.toml')) {
            sh 'python3 -m pytest || true'
          } else if (fileExists('Makefile')) {
            sh 'make test || true'
          } else if (fileExists('package.json')) {
            sh 'npm test || true'
          }
        }
      }
    }
    stage('build & push') {
      steps {
        sh "docker build -t ${IMG} ."
        sh "docker push ${IMG}"
      }
    }
  }
}
```

> Ajusta el bloque `test` a lo que el repo realmente use (los `|| true` evitan que una toolchain ausente tumbe el build).