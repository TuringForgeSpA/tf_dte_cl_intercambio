# tf_dte_cl_intercambio — Intercambio de DTE con clientes y proveedores

Complemento de [`tf_dte_cl`](../tf_dte_cl) para el **intercambio**: el canal
entre emisor y receptor, distinto del envío al SII, normado por la Res. Ex. SII
N° 45 de 2003 y la Ley 19.983.

## Alcance actual

**Como emisor**
- Arma un segundo sobre `EnvioDTE` dirigido al cliente (con su RUT en la carátula).
- Lo envía por correo con el XML y el PDF, una vez que el SII acepta el
  documento y solo a clientes con **correo de intercambio**.
- Botón para reenviar manualmente y estado visible en la factura.

**Como receptor**
- Recibe los sobres por correo en una casilla dedicada y registra cada documento.
- Responde automáticamente la **recepción del envío**, que es un acuse técnico:
  confirma que el archivo llegó y es válido, sin decidir nada comercial.
- Botón **Crear factura de proveedor**: deja un borrador con una línea por cada
  detalle del XML, con la descripción del proveedor sobre un producto genérico,
  el impuesto de compra por defecto en las líneas afectas y ninguno en las
  exentas. Si el total no coincide con el del DTE, lo avisa en el chatter y
  deja la factura en borrador igual.
- Si el proveedor no existe, lo crea con el RUT, la razón social y el giro del XML.

## Pendiente (segunda etapa)

- Aceptación y reclamo comercial (`validacion_comercial`).
- Acuse de recibo de mercaderías o servicios (`recepcion_mercaderias`), que
  habilita la cesión de la factura.
- Registro y consulta de reclamos ante el SII (`ingreso_reclamo_documento`,
  `consulta_reclamo_documento`).

Estas respuestas usan estructuras de la librería que conviene validar con un
XML recibido real antes de implementarlas.

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

| Ajuste | Para qué |
|---|---|
| Enviar el DTE al receptor | Activa el envío automático a los clientes con correo de intercambio |
| Producto para documentos recibidos | Producto genérico de las líneas de las facturas de proveedor |

En los contactos, el campo **Correo de intercambio DTE** define a quién se le
envía y desde dónde se espera recibir.

## Cómo se usa

**Clientes.** No hay que hacer nada: al aceptarse el DTE, un proceso automático
envía el correo dentro de los 15 minutos siguientes. El estado queda en la
pestaña DTE de la factura.

**Proveedores.** Los documentos llegan a *Facturación electrónica Chile >
Documentos recibidos*. Ahí se revisa el detalle y, con un botón, se crea la
factura de proveedor en borrador. Los sobres y sus respuestas quedan en
*Sobres recibidos*.

## Licencia

LGPL-3.
