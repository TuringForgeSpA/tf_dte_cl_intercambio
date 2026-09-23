# Manual de uso — Intercambio de DTE (tf_dte_cl_intercambio)

Módulo para Odoo 18, versión 18.0.5.0.0. Complementa a `tf_dte_cl`; su
manual cubre la emisión de documentos, y este, el intercambio con clientes y
proveedores.

---

## 1. Qué es el intercambio

Además de enviar cada DTE al SII, el emisor debe enviarlo a su cliente, y el
cliente puede responder. Ese canal entre empresas es el **intercambio**, normado
por la Res. Ex. SII N° 45 de 2003 y la Ley 19.983. Funciona por correo, a la
dirección que cada empresa registra en el SII como **correo de intercambio**.

El módulo cubre los dos lados:

| Lado | Qué hace |
|---|---|
| **Clientes** (documentos que emites) | Les envía el XML y el PDF, y sigue si aceptan o reclaman la factura |
| **Proveedores** (documentos que recibes) | Registra lo que llega, lo verifica en el SII, crea la factura de proveedor y permite aceptarla o reclamarla |

La aceptación y el reclamo con **efecto legal** se registran en el SII, no por
correo. El correo es el medio de entrega de los documentos.

---

## 2. Configuración inicial

### 2.1 Casilla de correo

Odoo necesita una **casilla propia** que pueda leer. La configuración
recomendada, sin costo adicional:

1. **Cuenta de Gmail dedicada** (por ejemplo, `intercambio.dte.tf@gmail.com`),
   usada solo para esto. Actívale la **verificación en dos pasos** y genera una
   **contraseña de aplicación** en `myaccount.google.com/apppasswords`. Guarda la
   clave de 16 caracteres: solo se muestra una vez.
2. **Grupo en Google Workspace** con tu dominio (por ejemplo,
   `intercambio@turingforge.cl`), con la cuenta de Gmail como miembro. En la
   configuración del grupo, permite **miembros externos** y que **cualquier
   persona externa pueda publicar**; si no, los correos de los proveedores
   rebotan.
3. **Registro en el SII**: actualiza el correo de intercambio de la empresa a la
   dirección del grupo. Los proveedores envían a la dirección que figura en el
   SII.

La casilla debe ser **exclusiva**: Odoo procesa todo lo que llega a ella, y un
correo sin DTE queda registrado como sobre con error.

### 2.2 Servidores de correo en Odoo

En *Ajustes > Técnico > Correo electrónico*:

| Servidor | Datos |
|---|---|
| **Entrante** | IMAP, `imap.gmail.com`, puerto 993 con SSL/TLS, la cuenta de Gmail y su contraseña de aplicación. En *Crear un nuevo registro*, elige **Sobre DTE recibido**. Presiona **Probar y confirmar**. |
| **Saliente** | `smtp.gmail.com`, puerto 465 con SSL (o 587 con STARTTLS), mismas credenciales. |

Revisa también que esté activa la tarea programada de Odoo que descarga los
correos entrantes.

Los correos saldrán con la dirección de Gmail como remitente. No afecta al
intercambio, porque el receptor valida el RUT del XML, no el remitente.

### 2.3 Ajustes del módulo

En *Contabilidad > Ajustes > Facturación electrónica Chile*, bloque
**Intercambio de documentos**:

| Ajuste | Para qué |
|---|---|
| **Enviar el DTE al receptor** | Envía automáticamente los documentos aceptados por el SII a los clientes con correo de intercambio |
| **Producto para documentos recibidos** | Producto genérico de las líneas de las facturas de proveedor. Viene creado como «Producto de proveedor» |
| **Responsable de documentos recibidos** | Recibe los avisos de plazo de los documentos que aún no tienen factura de proveedor |
| **Aviso de plazo (días antes)** | Anticipación de esos avisos; 2 por defecto |

En el mismo bloque de facturación electrónica, el **Correo DTE** de la compañía
debe ser la dirección de intercambio registrada en el SII.

### 2.4 Contactos

El campo **Correo de intercambio DTE** de cada cliente indica a dónde se envían
sus documentos. Si está vacío, al cliente no se le envía nada por intercambio.

---

## 3. Clientes: documentos que emites

### 3.1 Envío al cliente

No hay que hacer nada. Cuando el SII acepta una factura, nota o guía, un proceso
automático envía al cliente, dentro de los 15 minutos siguientes, un correo con:

- el **XML** en un sobre dirigido al cliente (con su RUT en la carátula);
- el **PDF** con el formato del SII.

En la pestaña **DTE** del documento, el bloque *Intercambio con el receptor*
muestra el estado, el correo usado y la fecha. El botón **Enviar al receptor**
permite reenviarlo a mano; no envía de nuevo un documento ya enviado.

El XML enviado queda adjunto al documento. El PDF no se adjunta, para que Odoo
no abra el visor de documentos junto a la factura.

### 3.2 Respuesta del cliente

En las **facturas 33 y 34 a crédito**, el cliente tiene 8 días corridos para
aceptarlas o reclamarlas en el SII. El módulo consulta esos registros cada 4
horas mientras corre el plazo, y los muestra en la pestaña DTE, bloque
*Respuesta del cliente*:

| Estado | Significado |
|---|---|
| Pendiente | Sin respuesta y dentro del plazo |
| Aceptado por el cliente | Aceptó el contenido |
| Acuse de recibo otorgado | Declaró recibidas las mercaderías o servicios |
| **Reclamado por el cliente** | Reclamó el contenido o la falta de mercaderías |
| Acuse de recibo presunto | Pasó el plazo sin reclamo: la ley presume el acuse de recibo |
| No aplica | Factura al contado, nota o guía: no admiten respuesta en el SII |

**Si un cliente reclama**, el módulo deja un mensaje en la factura y crea una
**actividad para el vendedor**. Corresponde revisar el caso y, si procede,
emitir una nota de crédito. El filtro **Reclamadas por el cliente** de la lista
de facturas muestra todas las que están en esa situación.

El botón **Consultar eventos del cliente** actualiza el estado a mano.

### 3.3 Respuestas por correo

Algunos clientes responden además por correo con un XML de respuesta. El módulo
lo reconoce, lo registra en *Sobres recibidos* como **Respuesta de un cliente**
y lo informa en la factura correspondiente. Estas respuestas son informativas:
lo que vale legalmente es lo registrado en el SII.

---

## 4. Proveedores: documentos que recibes

### 4.1 Cómo llegan

- **Por correo**, a la casilla de intercambio. Se descargan solos y aparecen en
  *Contabilidad > Configuración > Facturación electrónica Chile > Documentos
  recibidos*.
- **A mano**, con *Cargar XML de proveedor*, para documentos descargados desde el
  portal de un proveedor.

Cada correo queda como un **sobre recibido** (*Sobres recibidos*), y cada
documento que contiene, como un **documento recibido**. El módulo lee el sobre
estándar del SII, un DTE suelto o el formato propio de algunos proveedores de
facturación (por ejemplo, el que usa Acepta). Si un archivo trae documentos ya
registrados, se omiten con un aviso en el sobre.

### 4.2 Respuesta de recepción

Cuando el archivo recibido es un sobre estándar, el módulo responde
automáticamente al proveedor con la **respuesta de recepción**: un acuse técnico
que confirma que el archivo llegó y es válido. No implica aceptar la factura.

Los formatos sin carátula de envío (como el de Acepta) no admiten esta
respuesta; el sobre queda como *Sin respuesta de recepción*, y el documento se
procesa igual.

### 4.3 Verificación en el SII

Cada documento recibido se consulta en el SII para confirmar que existe y que
sus datos coinciden. El resultado se ve en el documento y en la lista:

| Resultado | Factura de proveedor |
|---|---|
| **Verificado en el SII** | Se crea normalmente |
| Datos no coinciden o modificado por una nota | Se crea, con aviso: revisa el documento antes de confirmar |
| **No recibido por el SII** | Bloqueada. Puede ser que el proveedor aún no lo envíe al SII: se sigue consultando durante 10 días desde la emisión |
| **Emisor no autorizado** o **anulado** | Bloqueada |

La verificación es automática cada 30 minutos; el botón **Verificar en el SII**
la hace a mano. El filtro **No verificados en el SII** muestra los pendientes.

### 4.4 Crear la factura de proveedor

Desde el documento recibido, botón **Crear factura de proveedor**. Queda un
**borrador** con:

- el proveedor (si no existe, se crea con el RUT, la razón social y el giro del XML);
- la fecha de emisión del documento;
- una línea por cada detalle del XML, con la descripción del proveedor sobre el
  producto genérico;
- los descuentos y recargos globales como líneas adicionales;
- el IVA de compra en las líneas afectas y ninguno en las exentas;
- el XML adjunto.

Si el total no coincide con el del documento, queda un aviso en el historial de
la factura para revisarla. Revisa también las cuentas y, si corresponde, cambia
el diario antes de confirmar.

> **Importante:** no uses el botón **Subir** de la lista de facturas de
> proveedor. Es una función estándar de Odoo que no reconoce el DTE chileno:
> solo adjunta el archivo a una factura en blanco. El documento debe entrar por
> *Documentos recibidos*.

Si un documento no corresponde facturarlo, el botón **Descartar** lo deja
registrado sin factura.

### 4.5 Aceptar o reclamar en el SII

Las **facturas 33 y 34 a crédito** recibidas se aceptan o reclaman en el SII
dentro de **8 días corridos** desde su recepción en el SII. Pasado ese plazo sin
reclamo, la ley presume el acuse de recibo y ya no se puede reclamar.

| Botón | Qué registra en el SII |
|---|---|
| **Aceptar en el SII** | Acepta el contenido y otorga el acuse de recibo |
| **Reclamar en el SII** | Reclamo al contenido, por falta parcial o por falta total de mercaderías. El motivo queda en el historial del documento |
| **Consultar SII** | Trae los eventos registrados, sin modificar nada |

Reglas del SII, que los botones respetan:

- un documento reclamado no se puede aceptar;
- un documento aceptado o con acuse de recibo no se puede reclamar;
- **las facturas al contado o sin costo no admiten aceptación ni reclamo**. En
  ellas no aparecen los botones, y el formulario lo explica.

El **plazo** que muestra el módulo es **estimado**: se calcula desde la fecha de
emisión, que es igual o anterior a la recepción en el SII. El plazo real nunca
es más corto que el mostrado. Cuando vence sin respuesta, la columna se marca en
rojo, y el filtro **Sin respuesta SII** muestra los pendientes.

### 4.6 Avisos de plazo

Cuando a una factura a crédito recibida le quedan pocos días (2 por defecto) sin
aceptación ni reclamo, el módulo crea una **actividad** que vence el mismo día
que el plazo. Se asigna a:

1. quien creó la factura de proveedor;
2. si todavía no existe, al **responsable de documentos recibidos** de Ajustes;
3. si no hay responsable, al administrador.

La actividad se cierra sola al aceptar o reclamar el documento.

---

## 5. Procesos automáticos

Se ven en *Ajustes > Técnico > Tareas programadas*:

| Tarea | Frecuencia | Qué hace |
|---|---|---|
| DTE: enviar documentos aceptados al receptor | 15 minutos | Envía el XML y el PDF a los clientes |
| DTE: responder la recepción de los sobres recibidos | 10 minutos | Envía la respuesta de recepción a los proveedores |
| DTE: verificar en el SII los documentos recibidos | 30 minutos | Consulta si los documentos recibidos existen en el SII |
| DTE: consultar la respuesta de los clientes en el SII | 4 horas | Trae las aceptaciones y reclamos de tus clientes |
| DTE: avisar plazos de documentos recibidos | Diaria | Crea las actividades de aviso de plazo |

Para probar algo sin esperar, abre la tarea y usa **Ejecutar manualmente**.

---

## 6. Problemas frecuentes

| Síntoma | Causa probable | Solución |
|---|---|---|
| No llegan documentos a Odoo | La prueba de conexión IMAP falla, o la tarea de descarga de correos está inactiva | Revisar el servidor entrante y la tarea programada |
| Los proveedores dicen que el correo rebota | El grupo de Google no admite remitentes externos | Permitir que cualquier persona externa publique en el grupo |
| Sobre con error «no contiene ningún DTE» | Llegó un correo sin XML de DTE a la casilla | Si no es un documento, se puede ignorar; mantener la casilla exclusiva |
| La factura de proveedor sale en blanco | Se usó el botón **Subir** de Odoo | Crear la factura desde *Documentos recibidos* |
| «No se puede crear la factura … no recibido por el SII» | El proveedor aún no envía el documento al SII, o no es auténtico | Esperar la siguiente verificación; si sigue, consultar al proveedor |
| No aparecen los botones de aceptar o reclamar | Factura al contado o sin costo, nota o guía | Es lo esperado: el SII no admite eventos en esos documentos |
| El SII responde «fuera de plazo» (código 8) | Pasaron los 8 días | El acuse de recibo ya se presume; no se puede reclamar |
| El SII responde código 17 | El certificado no es de un usuario autorizado por la empresa | Usar el certificado de un representante o usuario autorizado |
| El cliente no recibe el correo | No tiene correo de intercambio en su ficha | Completar el campo y usar **Enviar al receptor** |

---

## 7. Pendiente de documentar

- Confirmación con casos reales de la respuesta de un cliente a una factura a
  crédito y de una respuesta de cliente por correo.
