from data_loader import load_user_positions_from_folder, get_cash_balance_from_excel

print("=== Test data_loader ===")
positions = load_user_positions_from_folder()
print(f"Posiciones cargadas: {len(positions)}")
for p in positions:
    print(f"  {p['ticker']:6s} {p['shares']:>10.4f} acc @ ${p['entry_price']:>9.2f} (currency={p['currency']}, source={p['source']})")
print()
cash = get_cash_balance_from_excel()
print(f"Cash: {cash}")
