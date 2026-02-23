# Product Ideas

## Quick Wins (1-7 days)

1. **Batch and Purchase Metadata per Spool**
   - Add supplier, purchase date, batch/lot number, and invoice reference.
   - Benefit: better traceability and quality troubleshooting.

2. **Auto Reorder List from Thresholds**
   - Turn low-stock warnings into a concrete shopping list.
   - Include suggested quantity by material/color.

3. **Spool Lifecycle Status** ✅ implemented
   - Add statuses like `new`, `opened`, `dry-stored`, `humidity-risk`, `empty`, `archived`.
   - Benefit: better print reliability and spool hygiene control.

4. **Storage Mapping (Drybox/Rack Slot)**
   - Assign spools to physical locations (box/rack/slot).
   - Add filters by storage location.

## Mid-Term (1-4 weeks)

5. **WYSIWYG Label Designer**
   - Drag & drop fields (QR, spool id, material, color, remaining).
   - Optional logo and per-format typography settings.

6. **Flexible CSV/Excel Import Mapping**
   - Let users map incoming columns to app fields in the UI.
   - Benefit: easier onboarding from other tools.

7. **Usage Forecasting**
   - Predict how long current stock will last based on recent usage.
   - Add dashboard cards like “material runs out in X days”.

8. **Role-Based Access + Audit Trail**
   - Add roles (admin/user) and log critical changes.
   - Benefit: safer multi-user operation.

## Advanced Integrations

9. **API Keys + Webhooks**
   - Secure API access for slicers and external automations.
   - Emit events for booking, undo, low-stock, and reorder triggers.

10. **Mobile QR Scan Workflow**
   - Fast mobile page to scan spool QR and book usage/status changes.
   - Benefit: quicker updates directly at printer/storage.

## Suggested Next Step

Start with:
1. Auto reorder list
2. Spool lifecycle status
3. Storage mapping

These three are high-impact and relatively low-risk in the current architecture.
