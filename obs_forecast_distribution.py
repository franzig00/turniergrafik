import numpy as np
import polars as pl
from collections import defaultdict
from datetime import date
from argparse import ArgumentParser as ap
from global_functions import date_2_index, city_to_id, get_list_of_weekends
import db_read as dbr
import config_loader as cfg
import yaml
from itertools import product
import os
import re
import matplotlib.pyplot as plt
from scipy.stats import binned_statistic_2d
from decimal import Decimal, localcontext, ROUND_HALF_EVEN
import openpyxl
from openpyxl.styles import PatternFill


db = dbr.db()

# ------------------- YAML laden -------------------#
def _load_yaml_data(filepaths=['tabelle_obs_for.yml']):
    config_data = {}
    for filepath in filepaths:
        with open(filepath, 'r', encoding='utf-8') as f: data = yaml.safe_load(f)
        if data:
            config_data.update(data)
    return config_data

# ------------------ gewünschte Genauigkeit -------------#

DEC_QUANT = Decimal('0.001') 

# ------------------- Hilfsfunktionen -------------------#
def to_value(raw_value):
    if raw_value is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 12
        ctx.rounding = ROUND_HALF_EVEN
        d = Decimal(str(raw_value)) / Decimal('10')
        d = d.quantize(DEC_QUANT, rounding=ROUND_HALF_EVEN)
    return float(d)

def get_interval(value, ranges):
    for i, r in enumerate(ranges):
        a, b = r
        if a <= value <= b:
            return i, f"[{a}, {b}]"
    return None, None

intervals_cfg = _load_yaml_data(filepaths=['tabelle_obs_for.yml'])['Intervalle']
    
def get_obs_data(staedte, tage, elemente):
    obs_data = {}
    table_name = "wp_wetterturnier_obs"
    cursor = db.con.cursor(dictionary=True)
    for stadt in staedte:
        stationen = cfg.stationen[stadt]
        query = f"""
            SELECT station, betdate, p.paramName, value
            FROM {table_name} w
            INNER JOIN wp_wetterturnier_param p ON w.paramID = p.paramID
            WHERE station IN ({','.join(map(str, stationen))})
              AND betdate IN ({','.join(map(str, tage))})
              AND w.paramID IN ({','.join(map(str, elemente))})
            GROUP BY betdate, p.paramName, station
            ORDER BY betdate ASC, p.sort ASC, station ASC
        """
        cursor.execute(query)
        results = cursor.fetchall()
        nested = {}
        for row in results:
            betdate, param, raw_value = row['betdate'], row['paramName'], row['value']
            if raw_value is not None:
                nested.setdefault(betdate, {}).setdefault(param, []).append(to_value(raw_value))
        obs_data[cfg.id_zu_kuerzel[stadt]] = nested
    return obs_data   

                
def get_forecast_data(staedte, tage, elemente, users):
    forecast_data = {}
    table_name = "wp_wetterturnier_bets"
    cursor = db.con.cursor(dictionary=True)
    for stadt in staedte:
        query = f"""
            SELECT betdate, p.paramName, u.user_login, value
            FROM {table_name} w
            INNER JOIN wp_users u ON w.userID = u.ID
            INNER JOIN wp_wetterturnier_param p ON w.paramID = p.paramID
            WHERE w.cityID = {stadt}
              AND betdate IN ({','.join(map(str, tage))})
              AND w.paramID IN ({','.join(map(str, elemente))})
              AND w.userID IN ({','.join(map(str, users))})
            GROUP BY betdate, user_login, p.paramName
            ORDER BY betdate ASC, user_login ASC, p.sort ASC
        """
        cursor.execute(query)
        results = cursor.fetchall()
        nested = {}
        for row in results:
            betdate, user, param, raw_value = row['betdate'], row['user_login'], row['paramName'], row['value']
            if raw_value is not None:
                nested.setdefault(betdate, {}).setdefault(user, {})[param] = to_value(raw_value)
        forecast_data[cfg.id_zu_kuerzel[stadt]] = nested
    return forecast_data   


# ------------------- Klassenmittel -------------------#
def calc_class_means(intervals):
    means = []
    for r in intervals:
        with localcontext() as ctx:
            ctx.prec = 12
            ctx.rounding = ROUND_HALF_EVEN
            a, b = Decimal(str(r[0])), Decimal(str(r[1]))
            mean = (a + b) / 2
        means.append(mean)
    return means

# ------------------- Hauptprogramm -------------------
if __name__ == "__main__":
    # DB-Verbindung
    db = dbr.db()
    # Kommandozeilenargumente
    ps = ap()
    ps.add_argument("--von", type=str, default=cfg.datum_neue_elemente)
    ps.add_argument("--bis", type=str, default=cfg.endtermin)
    ps.add_argument("-p", "--params", type=str, default=",".join(cfg.elemente_archiv_neu))
    ps.add_argument("-c", "--cities", type=str, default=",".join(cfg.stadtnamen))
    ps.add_argument("-u", "--users", type=str, default=",".join(cfg.auswertungsteilnehmer))
    ps.add_argument("-d", "--days", type=str, default=",".join(cfg.auswertungstage))
    ps.add_argument("-v", "--verbose", action="store_true")
    ps = ps.parse_args()

    tdate_von = date_2_index(ps.von)
    tdate_bis = date_2_index(ps.bis)
    wochenendtage = get_list_of_weekends(tdate_von, tdate_bis)

    elemente_namen = [el for el in ps.params.split(",") if el in cfg.elemente_archiv_neu]
    elemente = db.get_param_ids(elemente_namen).values()
    staedte = [city_to_id(city, cfg) for city in ps.cities.split(",")]
    user_logins = ps.users.split(",")
    users_dict = db.get_user_ids(user_logins)
    users_dict_swapped = {v: k for k, v in users_dict.items()}
    users = users_dict.values()

    # Daten laden
    obs_data = get_obs_data(staedte, wochenendtage, elemente)
    forecast_data = get_forecast_data(staedte, wochenendtage, elemente, users)

    param_to_si_map = {name: unit for name, unit in zip(cfg.elemente_archiv_neu, cfg.elemente_einheiten_neu)}
    day_name = ps.days if ps.days in ["Sat","Sun"] else "All"
# ------------------- Daten kombinieren -------------------#
    combined_data = {}
    for city in obs_data:
        combined_data[city] = {}
        for betdate in obs_data[city]:
            combined_data[city][betdate] = {
                'o': obs_data[city][betdate],
                'f': {}
            }
            for user in users:
                user_login = users_dict_swapped[user]
                user_login_actual = cfg.teilnehmerumbenennung.get(user_login, user_login)
                try:
                    combined_data[city][betdate]['f'][user_login] = forecast_data[city][betdate].get(
                        user_login_actual,
                        {el: None for el in elemente_namen}
                    )
                except KeyError:
                    combined_data[city][betdate]['f'][user_login] = {el: None for el in elemente_namen}

# ------------------- Verarbeitung und Export -------------------#


for param in elemente_namen:
        obs_ranges_def = intervals_cfg.get(param, [])
        for_ranges_def = intervals_cfg.get(param, [])
        if not obs_ranges_def or not for_ranges_def:
            print(f"Skipping {param} due to missing ranges.")
            continue

        obs_class_means = calc_class_means(obs_ranges_def)
        for_class_means = calc_class_means(for_ranges_def)

        counts = defaultdict(int)
        values_by_bin = defaultdict(list)
        for city, city_data in combined_data.items():
            for betdate, data in city_data.items():
                obs_vals_list = data['o'].get(param, [])
                valid_obs = [v for v in obs_vals_list if v is not None]
                if not valid_obs:
                    continue
                obs_max = max(valid_obs)
                obs_idx, _ = get_interval(obs_max, obs_ranges_def)
                if obs_idx is None: continue
                obs_range_key = tuple(obs_ranges_def[obs_idx])

                for user, fvals in data['f'].items():
                    fcast_val = fvals.get(param)
                    if fcast_val is None: continue
                    f_idx, _ = get_interval(fcast_val, for_ranges_def)
                    if f_idx is None: continue
                    for_range_key = tuple(for_ranges_def[f_idx])
                    counts[(obs_range_key, for_range_key)] += 1
                    values_by_bin[(obs_range_key, for_range_key)].append((obs_max, fcast_val))

    if ps.verbose:
        print(f"{param} Obs outside ranges:", obs_missing)
        print(f"{param} For outside ranges:", for_missing)

    # ------------------- Klassenmittel -------------------#
        n_rows = len(obs_ranges_def)
        n_cols = len(for_ranges_def)

        # ------------------- Ausgabeverzeichnis ------------------- #
        all_city_str = "_".join(re.sub(r'[\\/:"*?<>|\s]+', '_', c) for c in combined_data.keys())
        outdir = os.path.join("distribution_outputs", all_city_str)
        os.makedirs(outdir, exist_ok=True)

        users_set = set(u for city_data in combined_data.values()
                        for betdate, data in city_data.items()
                        for u in data.get('f', {}).keys())
        user_str = "_".join(re.sub(r'[\\/:"*?<>|\s]+', '_', u) for u in users_set)

        outfile_xlsx = os.path.join(outdir, f"distribution_{all_city_str}_{param}_{user_str}_{day_name}.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active

        # ------------------- Kopfzeilen ------------------- #
        ws.cell(row=1, column=1, value="Kl")
        for j, for_r in enumerate(for_ranges_def):
            ws.cell(row=1, column=j+2, value=str(for_r[1]))

        blue_fill = PatternFill(start_color="ADD8E6", end_color="ADD8E6", fill_type="solid")
        orchid_fill = PatternFill(start_color="DA70D6", end_color="DA70D6", fill_type="solid")

        # ------------------- Matrix füllen ------------------- #
        matrix_counts = [[len(values_by_bin.get((tuple(obs_r), tuple(for_r)), []))
                          for for_r in for_ranges_def] for obs_r in obs_ranges_def]

        matrix_total_fc = [[sum(f for _, f in values_by_bin.get((tuple(obs_r), tuple(for_r)), []))
                            for for_r in for_ranges_def] for obs_r in obs_ranges_def]

        # ------------------- Zeilenwerte eintragen ------------------- #
        for i, obs_r in enumerate(obs_ranges_def):
            ws.cell(row=i+2, column=1, value=str(obs_r[1]))  # Obs-Klasse
            row_vals = matrix_counts[i]

            for j, count in enumerate(row_vals):
                cell = ws.cell(row=i+2, column=j+2, value=count)
                if i == j:
                    cell.fill = blue_fill

            # Row_Sum & Row_Mean
            row_sum = sum(row_vals)
            row_mean = round(sum(row_vals)/len(row_vals), 2) if row_vals else 0.0
            ws.cell(row=i+2, column=n_cols+2, value=row_sum)
            #ws.cell(row=i+2, column=n_cols+3, value=row_mean)

            # MFc pro Obs-Klasse
            total_fc_obsclass = [f for for_r in for_ranges_def for _, f in values_by_bin.get((tuple(obs_r), tuple(for_r)), [])]
            if total_fc_obsclass:
                if param in ["RR1", "RR24"] and sum(total_fc_obsclass)/len(total_fc_obsclass) < 1:
                    mean_fc_obsclass = round(sum(total_fc_obsclass)/len(total_fc_obsclass), 2)
                else:
                    mean_fc_obsclass = round(sum(total_fc_obsclass)/len(total_fc_obsclass), 1)
            else:
                mean_fc_obsclass = "NIL"
            ws.cell(row=i+2, column=n_cols+7, value=mean_fc_obsclass)

            # MOb pro Obs-Klasse
            obs_vals_in_class = [o for for_r in for_ranges_def for o, _ in values_by_bin.get((tuple(obs_r), tuple(for_r)), [])]
            if obs_vals_in_class:
                mob = round(sum(obs_vals_in_class)/len(obs_vals_in_class), 2)
            else:
                mob = "NIL"
                
            if total_fc_obsclass:
                forc = round(sum(obs_vals_in_class)/len(obs_vals_in_class), 2)
            else:
                forc = "NIL"
            ws.cell(row=i+2, column=n_cols+8, value=forc)

        # ------------------- Spaltenwerte eintragen ------------------- #
        for j, for_r in enumerate(for_ranges_def):
            col_vals = [matrix_counts[i][j] for i in range(n_rows)]
            col_sum = sum(col_vals)
            col_mean = round(sum(col_vals)/len(col_vals), 2) if col_vals else 0.0

            ws.cell(row=n_rows+2, column=j+2, value=col_sum)      # Col_Sum
           # ws.cell(row=n_rows+3, column=j+2, value=col_mean)     # Col_Mean

        # ------------------- Gesamt-Summe ------------------- #
        total_sum = sum(sum(row) for row in matrix_counts)
        ws.cell(row=n_rows+2, column=n_cols+2, value=total_sum).fill = orchid_fill

        # ------------------- Kopfzeilen für Zusatzzeilen/Spalten ------------------- #
        ws.cell(row=n_rows+2, column=1, value="Row_Sum")
        #ws.cell(row=n_rows+3, column=1, value="Row_Mean")
        ws.cell(row=1, column=n_cols+2, value="Col_Sum")
       # ws.cell(row=1, column=n_cols+3, value="Col_Mean")
        ws.cell(row=1, column=n_cols+7, value="MFc")
        #ws.cell(row=1, column=n_cols+5, value="MOb")
        ws.cell(row=1, column=n_cols+8, value="MOb")

        # ------------------- Speichern ------------------- #
        wb.save(outfile_xlsx)
        print(f"Excel table saved: {outfile_xlsx}")


    # --- ASCII Export ---
    asc_outfile = os.path.join(outdir, f"distribution_{all_city_str}_{param}_{user_str}_{day_name}.asc")
    col_widths_asc = [5, 6, 6, 4]
    headers = ["Kl", "MFc", "MOb", "#"]
    asc_lines = ["  ".join(f"{h:>{w}}" for h, w in zip(headers, col_widths_asc)),
                 "  ".join("-"*w for w in col_widths_asc)]
    for obs_r in obs_ranges_def:
        lower, upper = obs_r
        combined_vals = []
        for for_r in for_ranges_def:
            combined_vals.extend(values_by_bin.get((tuple(obs_r), tuple(for_r)), []))
        count = len(combined_vals)
        mean_fc = sum(v for (_, v) in combined_vals) / count if count else 0.0
        mean_obs = sum(o for (o, _) in combined_vals) / count if count else 0.0
        asc_lines.append("  ".join([
            f"{upper:>{col_widths_asc[0]}.1f}",
            f"{mean_fc:>{col_widths_asc[1]}.2f}",
            f"{mean_obs:>{col_widths_asc[2]}.2f}",
            f"{count:>{col_widths_asc[3]}}"
        ]))
    with open(asc_outfile, "w", encoding="utf-8") as f:
        f.write("\n".join(asc_lines))

from collections import defaultdict
from scipy.stats import linregress
import matplotlib.pyplot as plt
import os

for param in elemente_namen:
    obs_vals, fcast_vals = [], []
    counts = defaultdict(int)

    # --- Daten sammeln und Binning über Intervalle ---
    for city, city_data in combined_data.items():
        for betdate, data in city_data.items():
            obs_list = data["o"].get(param, [])
            if not obs_list:
                continue
            obs_max = max([v for v in obs_list if v is not None])
            
            for user, fvals in data["f"].items():
                fcast_val = fvals.get(param)
                if fcast_val is None:
                    continue

                # Intervalle über get_interval()
                obs_idx, _ = get_interval(obs_max, intervals_cfg.get(param, []))
                f_idx, _ = get_interval(fcast_val, intervals_cfg.get(param, []))
                if obs_idx is None or f_idx is None:
                    continue

                obs_range_key = tuple(intervals_cfg[param][obs_idx])
                f_range_key = tuple(intervals_cfg[param][f_idx])

                counts[(obs_range_key, f_range_key)] += 1
                obs_vals.append(obs_max)
                fcast_vals.append(fcast_val)

    if len(obs_vals) < 2:
        print(f"Not enough data for {param} to plot.")
        continue

    # --- Heatmap-Zuweisung (absolute Häufigkeiten pro Punkt) ---
    z = []
    for o, f in zip(obs_vals, fcast_vals):
        for (obs_bin, f_bin), count in counts.items():
            if obs_bin[0] <= o <= obs_bin[1] and f_bin[0] <= f <= f_bin[1]:
                z.append(count)
                break
        else:
            z.append(0)

    # --- Regression ---
    slope, intercept, r_value, p_value, std_err = linregress(obs_vals, fcast_vals)

    # --- Scatterplot ---
    fig, ax = plt.subplots(figsize=(16, 10))
    scatter = ax.scatter(obs_vals, fcast_vals, c=z, s=50, cmap='jet', alpha=0.7)

    min_val = min(min(obs_vals), min(fcast_vals))
    max_val = max(max(obs_vals), max(fcast_vals))
    ax.plot([min_val, max_val], [min_val, max_val], 'k--', label="Obs = Forecast")
    ax.plot([min_val, max_val],
            [intercept + slope*min_val, intercept + slope*max_val],
            'r-', label=f"y = {slope:.2f}x + {intercept:.2f}, R²={r_value**2:.2f}")

    # --- Achsenticks auf Basis der Intervalle ---
    if param in intervals_cfg:
        obs_ticks = [interval[1] for interval in intervals_cfg[param]]  # obere Grenze der Obs-Intervalle
        fcast_ticks = [interval[1] for interval in intervals_cfg[param]]  # obere Grenze der Forecast-Intervalle
        ax.set_xticks(obs_ticks)
        ax.set_yticks(fcast_ticks)
        ax.set_xticklabels([str(interval[1]) for interval in intervals_cfg[param]], rotation=45)
        ax.set_yticklabels([str(interval[1]) for interval in intervals_cfg[param]])
        # Für RR1 und RR24 

    # --- Achsenbeschriftung & Titel ---
    si_element = param_to_si_map.get(param, "")
    ax.set_xlabel(f"Observation ({param}) [{si_element}]")
    ax.set_ylabel(f"Forecast ({param}) [{si_element}]")
    ax.set_title(f"Scatterplot with absolute frequency for cities: {', '.join(city)}")
    ax.grid(True)
    ax.legend()

    # --- Colorbar ---
    cbar = fig.colorbar(scatter, ax=ax, location='right')
    cbar.set_label('Absolute frequency (counts per bin)')

    plt.tight_layout()
    plot_filename = os.path.join(outdir, f"scatter_absfreq_{city}_{param}_{user_str}.png")
    plt.savefig(plot_filename, dpi=300)
    plt.close(fig)
    print(f"Scatterplot saved for {param}: {plot_filename}")




















