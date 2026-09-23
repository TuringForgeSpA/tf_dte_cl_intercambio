# -*- coding: utf-8 -*-
"""Configuración del intercambio.

Ruta real: models/res_company.py
"""
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    tf_dte_cl_exchange_auto_send = fields.Boolean(
        string='Enviar el DTE al receptor', default=True,
        help='Envía por correo el XML y el PDF a los clientes con correo de intercambio, '
             'una vez que el SII acepta el documento.',
    )
    tf_dte_cl_exchange_product_id = fields.Many2one(
        'product.product', string='Producto para documentos recibidos',
        default=lambda self: self.env.ref('tf_dte_cl_intercambio.product_exchange_generic',
                                          raise_if_not_found=False),
        help='Producto usado en las líneas de las facturas de proveedor creadas desde un DTE recibido; '
             'la descripción es la del proveedor.',
    )
    tf_dte_cl_exchange_alert_days = fields.Integer(
        string='Aviso de plazo (días antes)', default=2,
        help='Días antes del vencimiento del plazo de 8 días en que se avisa de una factura recibida '
             'sin aceptación ni reclamo en el SII.',
    )
    tf_dte_cl_exchange_responsible_id = fields.Many2one(
        'res.users', string='Responsable de documentos recibidos',
        help='Recibe el aviso de plazo cuando el documento aún no tiene factura de proveedor. Si ya la '
             'tiene, el aviso va al usuario que la creó.',
    )
