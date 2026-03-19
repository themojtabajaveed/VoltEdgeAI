import pandas as pd
from db import init_db, SessionLocal, FundamentalUniverse

"""
PHASE_I_RULES summary for VoltEdgeAI Fundamental Universe:
1. Universe: Nifty 500 only (assumed pre-filtered in fundamentals.csv).
2. Growth: TTM EPS growth > 20%.
3. Consistency: 3Q average EPS / Sales / Margin YoY growth > 0.
4. Capital Efficiency: ROCE > 15%.
5. Debt Management: D/E < 0.8.
6. Promoter Support: Promoter pledge < 5%.
7. Basic Governance Check: Clean track record (assumed).
"""

def main():
    print("Initializing Database...")
    init_db()

    csv_path = "data/fundamentals.csv"
    
    try:
        df = pd.read_csv(csv_path)
    except FileNotFoundError:
        print(f"Error: Could not find {csv_path}. Please ensure it exists.")
        return
    except Exception as e:
        print(f"Error reading {csv_path}: {e}")
        return

    print(f"Read {len(df)} rows from {csv_path}. Evaluating Phase I rules...\n")

    passed_count = 0
    symbols_passed = set()

    with SessionLocal() as session:
        for index, row in df.iterrows():
            symbol = str(row.get('symbol')).strip()
            if not symbol or symbol == 'nan':
                continue

            # Extract row values safely
            eps_ttm = float(row.get('eps_growth_ttm', 0))
            
            eps_q1 = float(row.get('eps_growth_yoy_q1', 0))
            eps_q2 = float(row.get('eps_growth_yoy_q2', 0))
            eps_q3 = float(row.get('eps_growth_yoy_q3', 0))
            eps_avg_3q = (eps_q1 + eps_q2 + eps_q3) / 3.0
            
            sales_q1 = float(row.get('sales_growth_yoy_q1', 0))
            sales_q2 = float(row.get('sales_growth_yoy_q2', 0))
            sales_q3 = float(row.get('sales_growth_yoy_q3', 0))
            sales_avg_3q = (sales_q1 + sales_q2 + sales_q3) / 3.0
            
            margin_q1 = float(row.get('margin_growth_yoy_q1', 0))
            margin_q2 = float(row.get('margin_growth_yoy_q2', 0))
            margin_q3 = float(row.get('margin_growth_yoy_q3', 0))
            margin_avg_3q = (margin_q1 + margin_q2 + margin_q3) / 3.0

            roce = float(row.get('roce', 0))
            de_ratio = float(row.get('de_ratio', 0))
            pledge_pct = float(row.get('promoter_pledge_pct', 0))
            
            # Phase I Rules Check
            if eps_ttm <= 20:
                continue
            if eps_avg_3q <= 0:
                continue
            if sales_avg_3q <= 0:
                continue
            if margin_avg_3q <= 0:
                continue
            if roce <= 15:
                continue
            if de_ratio >= 0.8:
                continue
            if pledge_pct >= 5:
                continue
                
            # Passed all Phase I rules
            passed_count += 1
            symbols_passed.add(symbol)
            
            # Upsert into DB
            db_row = session.query(FundamentalUniverse).filter_by(symbol=symbol).first()
            if not db_row:
                db_row = FundamentalUniverse(symbol=symbol)
                session.add(db_row)
            
            # Update fields
            db_row.name = str(row.get('name', ''))
            db_row.market_cap = float(row.get('market_cap_cr', 0))
            db_row.eps_growth_ttm = eps_ttm
            db_row.eps_growth_qoq_3q = eps_avg_3q
            db_row.sales_growth_qoq_3q = sales_avg_3q
            db_row.margin_growth_qoq_3q = margin_avg_3q
            db_row.roce = roce
            db_row.roe = float(row.get('roe', 0))
            db_row.de_ratio = de_ratio
            db_row.promoter_pledge_pct = pledge_pct
            db_row.institutional_holding_pct = float(row.get('institutional_holding_pct', 0))
            
            db_row.is_active = True
            db_row.macro_ok = True
            
        # Deactivate any rows not in this current successful sweep
        all_db_symbols = session.query(FundamentalUniverse).all()
        deactivated_count = 0
        for db_row in all_db_symbols:
            if db_row.symbol not in symbols_passed and db_row.is_active:
                db_row.is_active = False
                deactivated_count += 1
                
        session.commit()
        
        print(f"Successfully evaluated and updated {passed_count} symbol(s) passing Phase I rules.")
        if deactivated_count > 0:
            print(f"Deactivated {deactivated_count} symbol(s) that no longer pass the rules.")

if __name__ == "__main__":
    main()
