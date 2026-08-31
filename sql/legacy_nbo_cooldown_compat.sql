-- A same-day NBO replay must not treat its own snapshot as a prior exposure.
-- Accepted offers do enter cooldown immediately, including on the acceptance date.
CREATE OR REPLACE VIEW rpt.vw_NBO_Cooldown AS
WITH exposures AS (
    SELECT ns.ClientID, ns.ProductID, ns.SnapshotDate AS ExposureDate
    FROM ml.NBO_Scores ns
    WHERE ns.EligibleFlag = TRUE
      AND ns.SnapshotDate < CURRENT_DATE
      AND ns.SnapshotDate >= CURRENT_DATE - INTERVAL '60 days'
    UNION ALL
    SELECT a.ClientID, a.ProductID, a.OfferDate AS ExposureDate
    FROM public.NBO_Accepted a
    WHERE a.AcceptedFlag = TRUE
      AND a.OfferDate >= CURRENT_DATE - INTERVAL '60 days'
), latest AS (
    SELECT ClientID, ProductID, MAX(ExposureDate) AS LastOfferedDate
    FROM exposures GROUP BY ClientID, ProductID
)
SELECT l.ClientID, l.ProductID, p.CooldownDays, l.LastOfferedDate,
       CURRENT_DATE - l.LastOfferedDate AS DaysSinceOffered
FROM latest l
JOIN public.Products p ON p.ProductID=l.ProductID
WHERE CURRENT_DATE - l.LastOfferedDate < p.CooldownDays;

