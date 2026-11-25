"""The KPI library's chart proposals (kpilib.py)."""
import kpilib

# RKS Revo Hospitality Group, Business Central 25 Nov 2026: every posting account (No, Income/Balance)
RKS = """101 B,109 B,203 B,208 B,213 B,2131 B,2134 B,2135 B,215 B,2181 B,2182 B,21822 B,21823 B,2188 B,231 B,261 B,2803 B,
2808 B,2813 B,28131 B,28134 B,28135 B,2815 B,28181 B,28182 B,28188 B,311 B,312 B,3123 B,3124 B,3126 B,327 B,351 B,35801 B,
371 B,372 B,3723 B,3726 B,3727 B,375 B,401 B,40102 B,411 B,418 B,41801 B,41802 B,41803 B,41804 B,421 B,431 B,432 B,442 B,
444 B,4456 B,4457 B,4458 B,4461 B,447 B,449 B,461 B,467 B,467011 B,467012 B,467013 B,467014 B,467015 B,467016 B,467017 B,
468 B,46801 B,46802 B,46803 B,512100 B,512101 B,5121011 B,512400 B,512401 B,5124010 B,512402 B,512403 B,512404 B,512405 B,
512406 B,512407 B,512408 B,512409 B,51241 B,51242 B,513400 B,531401 B,531402 B,53141 B,5421 B,581 B,582 B,6011 I,60111 I,
6012 I,60121 I,60122 I,60123 I,601231 I,60124 I,601241 I,60125 I,6013 I,602 I,6027 I,60271 I,6034 I,6036 I,604 I,60401 I,
60402 I,60403 I,605 I,6051 I,615 I,61501 I,6151 I,6152 I,6153 I,616 I,618 I,622 I,624 I,62401 I,625 I,626 I,627 I,6271 I,
6276 I,628 I,632 I,6321 I,6322 I,634 I,636 I,6361 I,6362 I,638 I,641 I,64102 I,64103 I,6411 I,6412 I,64122 I,6413 I,64131 I,
6414 I,644 I,648 I,649 I,650 I,654 I,657 I,658 I,65801 I,65802 I,65803 I,65804 I,6581 I,666 I,667 I,668 I,669 I,66901 I,
67205 I,6813 I,68188 I,687 I,704 I,705 I,707 I,70711 I,707123 I,707126 I,7072 I,708 I,75 I,751 I,76 I,761 I,769 I,76901 I,
77 I,7903 I,81 B,890 B"""


def test_rks_has_a_proposal_of_its_own_chart_with_every_account_on_its_statement(monkeypatch):
    monkeypatch.setattr(kpilib, "_read", lambda: {})
    m = kpilib.mapping("RKS Revo Hospitality Group")
    assert m["proposal"] and m["status"] == "Proposed" and m["proposal_of"] == "its own chart (RKS)"
    accts = [a.split() for a in RKS.replace("\n", "").split(",")]
    assert len(accts) == 189
    of, line = kpilib.cluster_of(m), kpilib.classifier(m)
    assert [no for no, _s in accts if not of(no)] == []
    assert [no for no, s in accts if (kpilib.LINE[line(no)]["statement"] == "pl") != (s == "I")] == []
    assert of("6361") == "other_exp" and of("68188") == "da_pack" and of("6036") == "cogs_pack" and of("46802") == "loans"
    assert line("705") == "revenue" and line("641") == "labour" and line("636") == "occupancy" and line("667") == "finance"


def test_a_saved_mapping_without_the_pack_takes_its_own_chart_pack():
    saved = {"status": "Reviewed", "rules": {"revenue": "70*", "cash": "51*"}}
    r = kpilib._to_pack(saved, kpilib.RKS_MODEL)
    assert r["rules"]["revenue"] == "" and r["rules"]["other_exp"] == kpilib.RKS_PACK["other_exp"] and r["rules"]["cash"] == "51*"
