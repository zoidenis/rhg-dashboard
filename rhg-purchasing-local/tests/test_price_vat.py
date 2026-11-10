"""Price lists that include VAT: by the card in BC, or by RHG's decision in config; never contacts BC."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bc_client
import config


class Fake(bc_client.BCClient):
    def __init__(self, heads):
        self.heads = heads

    def _url(self, company, entity):
        return entity

    def _get_all(self, url, params=None):
        if isinstance(self.heads, Exception):
            raise self.heads
        return self.heads

    def vat_rates(self, company, max_age=0):
        return {("V", "TVSH18"): 18.0, ("V", "TVSH20"): 20.0}

    def vendor_cards(self, company, max_age=0):
        return [{"No": "F0068", "VAT_Bus_Posting_Group": "V"}]

    def item_catalog(self, company, max_age=0):
        return {"A00302": {"vat_prod": "TVSH18"}}


ROWS = [{"Price_List_Code": "P00001"}]
RKS = "RKS Revo Hospitality Group"


class PriceVat(unittest.TestCase):
    def test_rks_p00001_includes_vat_by_decision(self):
        with patch.object(config, "BC_PRICE_LISTS_INCL_VAT", {(RKS, "P00001")}):
            vat_of, note = Fake([{"Code": "P00001", "PriceIncludesVAT": False}])._price_vat(RKS, ROWS)
            self.assertEqual(vat_of("P00001", "F0068", "A00302"), 18.0)
            self.assertAlmostEqual(0.52083 / (1 + vat_of("P00001", "F0068", "A00302") / 100), 0.44138, places=5)
            self.assertIn("RHG's decision", note)
            vat_of, note = Fake([{"Code": "P00001", "PriceIncludesVAT": False}])._price_vat("TBM", ROWS)
            self.assertEqual(vat_of("P00001", "F0068", "A00302"), 0.0)       # the same code in another company

    def test_card_ticked_needs_no_decision_and_unreadable_card_keeps_it(self):
        with patch.object(config, "BC_PRICE_LISTS_INCL_VAT", {(RKS, "P00001")}):
            vat_of, note = Fake([{"Code": "P00001", "PriceIncludesVAT": True}])._price_vat(RKS, ROWS)
            self.assertEqual(vat_of("P00001", "F0068", "A00302"), 18.0)
            self.assertNotIn("RHG's decision", note)
            vat_of, _ = Fake(bc_client.BCError("no card"))._price_vat(RKS, ROWS)
            self.assertEqual(vat_of("P00001", "F0068", "A00302"), 18.0)

    def test_default_setting(self):
        self.assertIn((RKS, "P00001"), config.BC_PRICE_LISTS_INCL_VAT)


if __name__ == "__main__":
    unittest.main()
