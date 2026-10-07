"""Normalize legacy exchange percentage units using each filing's total row."""
import polars as pl


def percentages(frame: pl.DataFrame) -> pl.DataFrame:
    if frame.is_empty():
        return frame
    ratios=frame.filter((pl.col('category')=='total') & (pl.col('pct_of_total')==1.0)
                        & (pl.col('shares')>0)).select('filing_id').unique()
    return (frame.join(ratios.with_columns(pl.lit(True).alias('_ratio')),
                       on='filing_id',how='left')
            .with_columns(pl.when(pl.col('_ratio').fill_null(False))
                .then(pl.col('pct_of_total')*100).otherwise(pl.col('pct_of_total'))
                .alias('pct_of_total'),
                # A reported pledged share count is an unambiguous denominator check.
                pl.when((pl.col('pledged_shares').is_not_null()) & (pl.col('shares')>0))
                .then(100*pl.col('pledged_shares')/pl.col('shares'))
                .otherwise(pl.col('pledged_pct')).alias('pledged_pct'))
            .drop('_ratio'))
