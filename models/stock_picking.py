# -*- coding: utf-8 -*-
"""Guías de despacho recibidas asociadas a las recepciones de inventario.

Ruta real: models/stock_picking.py
"""
from odoo import api, fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    # Solo para usuarios de facturación: los documentos recibidos no son visibles en inventario.
    tf_dte_cl_received_guide_ids = fields.One2many(
        'tf_dte_cl.received', 'picking_id', string='Guías del proveedor',
        groups='account.group_account_invoice',
    )
    tf_dte_cl_received_guide_count = fields.Integer(
        compute='_compute_tf_dte_cl_received_guide_count', groups='account.group_account_invoice',
    )

    @api.depends('tf_dte_cl_received_guide_ids')
    def _compute_tf_dte_cl_received_guide_count(self):
        for picking in self:
            picking.tf_dte_cl_received_guide_count = len(picking.tf_dte_cl_received_guide_ids)
