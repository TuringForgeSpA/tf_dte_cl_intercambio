# tf_dte_cl_intercambio — Intercambio de DTE con clientes y proveedores

Complemento de [`tf_dte_cl`](https://github.com/TuringForgeSpA/tf_dte_cl) para el **intercambio**: el canal
entre emisor y receptor, distinto del envío al SII, normado por la Res. Ex. SII
N° 45 de 2003 y la Ley 19.983.

## Alcance actual

**Como emisor**

- Arma un segundo sobre `EnvioDTE` dirigido al cliente (con su RUT en la carátula).
- Lo envía por correo con el XML y el PDF, una vez que el SII acepta el
  documento y solo a clientes con **correo de intercambio**.
- Botón para reenviar manualmente y estado visible en la factura.

**Como receptor**

- Recibe los documentos por correo en una casilla dedicada, o por carga manual,
  y registra cada uno. Lee el sobre `EnvioDTE` estándar, un DTE suelto o el
  envoltorio propio de un proveedor de facturación (por ejemplo, Acepta).
- Responde automáticamente la **recepción del envío** cuando el archivo es un
  sobre con carátula. Los formatos sin carátula no admiten esta respuesta,
  porque cita el ID y la firma del sobre.
- Botón **Crear factura de proveedor**: deja un borrador con una línea por cada
  detalle del XML, con la descripción del proveedor sobre un producto genérico,
  el impuesto de compra en las líneas afectas y ninguno en las exentas. Los
  descuentos y recargos globales se agregan como líneas. Si el total no
  coincide con el del DTE, lo avisa y deja el borrador igual. Crea el proveedor
  si no existe.

**Aceptación y reclamo ante el SII**

Según la Ley 19.983 (modificada por la Ley 20.956), el receptor tiene **8 días
corridos** desde la recepción del documento en el SII para reclamarlo; pasado
ese plazo, se presume otorgado el acuse de recibo. El registro se hace con el
servicio web del SII *Consulta y Registro de Aceptación/Reclamo a DTE recibido*
(v1.2), que solo opera con facturas (33, 34 y 43).

| Botón                  | Acciones en el SII                                                                                                          |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **Aceptar en el SII**  | `ACD` (acepta el contenido) y `ERM` (otorga el acuse de recibo)                                                             |
| **Reclamar en el SII** | `RCD` (reclamo al contenido), `RFP` o `RFT` (falta parcial o total de mercaderías), con un motivo que queda en el historial |
| **Consultar SII**      | Trae los eventos registrados y actualiza el estado del documento                                                            |

El SII no permite aceptar un documento reclamado ni reclamar uno aceptado o con
acuse de recibo; los botones se ocultan según el último evento.

Tampoco admite eventos en facturas **al contado o sin costo** (forma de pago 1
o 3 en el DTE): el SII responde con el código 27. En esos documentos no se
muestran los botones ni el plazo, porque el acuse de recibo de la Ley 19.983
aplica a las ventas a crédito.

El plazo mostrado es **estimado**: se calcula desde la fecha de emisión, porque
la librería no consulta la fecha de recepción en el SII. Como la emisión es
igual o anterior a la recepción, el plazo real nunca es más corto que el mostrado.

## Pendiente

- Respuestas comerciales por correo en XML (`RespuestaDTE`), opcionales: la
  aceptación y el reclamo con efecto legal se registran en el SII.
- Consulta de la fecha exacta de recepción en el SII, para mostrar el plazo real.

## Configuración del correo

El intercambio necesita una **casilla real** que Odoo pueda leer por IMAP. Un
grupo de Google Workspace no sirve, porque no es una casilla; un alias sí.

Opción recomendada, sin cuentas adicionales:

1. En Google Workspace, crear una **regla de enrutamiento** para la dirección
   registrada en el SII (por ejemplo `facturas@`) que entregue **también** una
   copia a una casilla dedicada (por ejemplo `dte@` en un servidor propio).
2. Dejar la dirección registrada en el SII como **«Enviar como»** en la cuenta
   que use Odoo, para que las respuestas salgan desde ella.
3. En Odoo, **Ajustes > Técnico > Servidores de correo entrante**: servidor IMAP
   apuntando a la casilla dedicada, con el modelo **Sobre DTE recibido**
   (`tf_dte_cl.exchange.envelope`).
4. Configurar el servidor de correo saliente con el SMTP de Google, usando una
   contraseña de aplicación u OAuth.

Ventaja de esta opción: el correo original queda en Google, así que ningún DTE
se pierde si falla el procesamiento.

## Ajustes del módulo

En *Contabilidad > Ajustes > Facturación electrónica Chile*:

| Ajuste                             | Para qué                                                            |
| ---------------------------------- | ------------------------------------------------------------------- |
| Enviar el DTE al receptor          | Activa el envío automático a los clientes con correo de intercambio |
| Producto para documentos recibidos | Producto genérico de las líneas de las facturas de proveedor        |

En los contactos, el campo **Correo de intercambio DTE** define a quién se le
envía y desde dónde se espera recibir.

## Cómo se usa

**Clientes.** No hay que hacer nada: al aceptarse el DTE, un proceso automático
envía el correo dentro de los 15 minutos siguientes. El estado queda en la
pestaña DTE de la factura.

**Proveedores.** Los documentos llegan a *Facturación electrónica Chile >
Documentos recibidos* (o se suben con *Cargar XML de proveedor*). Ahí se
revisa el detalle, se crea la factura de proveedor en borrador y, dentro del
plazo, se acepta o reclama en el SII. El filtro *Sin respuesta SII* muestra los
documentos pendientes, y la columna del plazo se marca en rojo al vencer. Los
sobres y sus respuestas de recepción quedan en *Sobres recibidos*.

## Licencia

LGPL-3.
