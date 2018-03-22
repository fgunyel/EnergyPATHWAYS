__author__ = 'Ben Haley & Ryan Jones'

import os
from demand import Demand
import util
from outputs import Output
import shutil
import config as cfg
from supply import Supply
import pandas as pd
import logging
import shape
import pdb
from scenario_loader import Scenario
import copy
import numpy as np

class PathwaysModel(object):
    """
    Highest level classification of the definition of an energy system.
    """
    def __init__(self, scenario_id, api_run=False):
        self.scenario_id = scenario_id
        self.scenario = Scenario(self.scenario_id)
        self.api_run = api_run
        self.outputs = Output()
        self.demand = Demand(self.scenario)
        self.supply = None
        self.demand_solved, self.supply_solved = False, False

    def run(self, scenario_id, solve_demand, solve_supply, load_demand, load_supply, export_results, save_models, append_results):
        try:
            if solve_demand and not (load_demand or load_supply):
                self.calculate_demand(save_models)
            
            if not append_results:
                self.remove_old_results()

            # it is nice if when loading a demand side object to rerun supply, it doesn't re-output these results every time
            if self.demand_solved and export_results and not self.api_run and not (load_demand and solve_supply):
                self.export_result_to_csv('demand_outputs')

            if solve_supply and not load_supply:
                if load_demand:
                    # if we are loading the demand, we are changing the supply measures and want to reload our scenarios
                    self.scenario = Scenario(self.scenario_id)
                self.supply = Supply(self.scenario, demand_object=self.demand)
                self.calculate_supply(save_models)

            if load_demand and solve_supply:
                # we do this now because we delayed before
                self.export_result_to_csv('demand_outputs')

            if self.supply_solved and export_results and load_supply or solve_supply:
                self.supply.calculate_supply_outputs()
                self.pass_supply_results_back_to_demand()
                self.calculate_combined_results()
                self.outputs.electricity_reconciliation = self.demand.electricity_reconciliation # we want to write these to outputs
                self.export_result_to_csv('supply_outputs')
                self.export_result_to_csv('combined_outputs')
                self.export_io()
        except:
            # pickle the model in the event that it crashes
            if save_models:
                Output.pickle(self, file_name=str(scenario_id) + cfg.model_error_append_name, path=cfg.workingdir)
            raise

    def calculate_demand(self, save_models):
        self.demand.setup_and_solve()
        self.demand_solved = True
        if cfg.output_payback == 'true':
            if self.demand.d_all_energy_demand_payback is not None:
                self.calculate_d_payback()
                self.calculate_d_payback_energy()
        if save_models:
            Output.pickle(self, file_name=str(self.scenario_id) + cfg.demand_model_append_name, path=cfg.workingdir)

    def calculate_supply(self, save_models):
        if not self.demand_solved:
            raise ValueError('demand must be solved first before supply')
        logging.info('Configuring energy system supply')
        self.supply.add_nodes()
        self.supply.add_measures()
        self.supply.initial_calculate()
        self.supply.calculated_years = []
        self.supply.calculate_loop(self.supply.years, self.supply.calculated_years)
        self.supply.final_calculate()
        self.supply_solved = True
        if save_models:
            Output.pickle(self, file_name=str(self.scenario_id) + cfg.full_model_append_name, path=cfg.workingdir)
            # we don't need the demand side object any more, so we can remove it to save drive space
            if os.path.isfile(os.path.join(cfg.workingdir, str(self.scenario_id) + cfg.demand_model_append_name)):
                os.remove(os.path.join(cfg.workingdir, str(self.scenario_id) + cfg.demand_model_append_name))

    def pass_supply_results_back_to_demand(self):
        # we need to geomap to the combined output geography
        emissions_demand_link = cfg.geo.geo_map(self.supply.emissions_demand_link, cfg.supply_primary_geography, cfg.combined_outputs_geography, 'intensity')
        demand_emissions_rates = cfg.geo.geo_map(self.supply.demand_emissions_rates, cfg.supply_primary_geography, cfg.combined_outputs_geography, 'intensity')
        energy_demand_link = cfg.geo.geo_map(self.supply.energy_demand_link, cfg.supply_primary_geography, cfg.combined_outputs_geography, 'intensity')
        cost_demand_link = cfg.geo.geo_map(self.supply.cost_demand_link, cfg.supply_primary_geography, cfg.combined_outputs_geography, 'intensity')

        logging.info("Calculating link to supply")
        self.demand.link_to_supply(emissions_demand_link, demand_emissions_rates, energy_demand_link, cost_demand_link)
        if cfg.output_tco == 'true':
            if hasattr(self,'d_energy_tco'):
                self.demand.link_to_supply_tco(emissions_demand_link, demand_emissions_rates, cost_demand_link)
            else:
               print  "demand side has not been run with tco outputs set to 'true'"
        if cfg.output_payback == 'true':
            if hasattr(self,'demand.d_all_energy_demand_payback'):
                self.demand.link_to_supply_payback(emissions_demand_link, demand_emissions_rates, cost_demand_link)
            else:
               print  "demand side has not been run with tco outputs set to 'true'"
    
    def calculate_combined_results(self):
        logging.info("Calculating combined emissions results")
        self.calculate_combined_emissions_results()
        logging.info("Calculating combined cost results")
        self.calculate_combined_cost_results()
        logging.info("Calculating combined energy results")
        self.calculate_combined_energy_results()
        if cfg.output_tco == 'true':
            if self.demand.d_energy_tco is not None:
                self.calculate_tco()
        if cfg.output_payback == 'true':
            if self.demand.d_all_energy_demand_payback is not None:
                self.calculate_payback()

    def remove_old_results(self):
        folder_names = ['combined_outputs', 'demand_outputs', 'supply_outputs', 'dispatch_outputs']
        for folder_name in folder_names:
            folder = os.path.join(cfg.workingdir, folder_name)
            if os.path.isdir(folder):
                shutil.rmtree(folder)

    def export_result_to_csv(self, result_name):
        if result_name=='combined_outputs':
            res_obj = self.outputs
        elif result_name=='demand_outputs':
            res_obj = self.demand.outputs
        elif result_name=='supply_outputs':
            res_obj = self.supply.outputs
        else:
            raise ValueError('result_name not recognized')

        for attribute in dir(res_obj):
            if not isinstance(getattr(res_obj, attribute), pd.DataFrame):
                continue

            result_df = getattr(res_obj, 'return_cleaned_output')(attribute)
            keys = [self.scenario.name.upper(), cfg.timestamp]
            names = ['SCENARIO','TIMESTAMP']
            for key, name in zip(keys, names):
                result_df = pd.concat([result_df], keys=[key], names=[name])

            if attribute in ('hourly_dispatch_results', 'electricity_reconciliation', 'hourly_marginal_cost', 'hourly_production_cost'):
                # Special case for hourly dispatch results where we want to write them outside of supply_outputs
                Output.write(result_df, attribute + '.csv', os.path.join(cfg.workingdir, 'dispatch_outputs'))
            else:
                Output.write(result_df, attribute+'.csv', os.path.join(cfg.workingdir, result_name))
        
    def calculate_tco(self):
        cost_unit = cfg.cfgfile.get('case','currency_year_id') + " " + cfg.cfgfile.get('case','currency_name')
        initial_vintage = min(cfg.supply_years)
        supply_side_df = self.demand.outputs.demand_embodied_energy_costs_tco
        supply_side_df = supply_side_df[supply_side_df.index.get_level_values('vintage')>=initial_vintage]
        demand_side_df = self.demand.d_levelized_costs_tco
        demand_side_df.columns = ['value']
        demand_side_df = demand_side_df[demand_side_df.index.get_level_values('vintage')>=initial_vintage]
        service_demand_df = self.demand.d_service_demand_tco
        service_demand_df = service_demand_df[service_demand_df.index.get_level_values('vintage')>=initial_vintage]
        keys = ['SUPPLY-SIDE', 'DEMAND-SIDE']
        names = ['COST TYPE']
        self.outputs.c_tco = pd.concat([util.DfOper.divi([supply_side_df,util.remove_df_levels(service_demand_df,'unit')]),
                                        util.DfOper.divi([demand_side_df,util.remove_df_levels(service_demand_df,'unit')])],
                                        keys=keys,names=names) 
        self.outputs.c_tco = self.outputs.c_tco.replace([np.inf,np.nan],0)
        self.outputs.c_tco[self.outputs.c_tco<0]=0        
        for sector in self.demand.sectors.values():
          for subsector in sector.subsectors.values():
                if hasattr(subsector,'service_demand') and hasattr(subsector,'stock'):
                    indexer = util.level_specific_indexer(self.outputs.c_tco,'subsector',subsector.id)
                    self.outputs.c_tco.loc[indexer,'unit'] = subsector.service_demand.unit.upper()
        self.outputs.c_tco = self.outputs.c_tco.set_index('unit',append=True)
        self.outputs.c_tco.columns = [cost_unit.upper()]
        self.outputs.c_tco= self.outputs.c_tco[self.outputs.c_tco[cost_unit.upper()]!=0]
        self.outputs.c_tco = self.outputs.return_cleaned_output('c_tco')
        
    def calculate_payback(self):
        cost_unit = cfg.cfgfile.get('case','currency_year_id') + " " + cfg.cfgfile.get('case','currency_name')
        initial_vintage = min(cfg.supply_years)
        supply_side_df = self.demand.outputs.demand_embodied_energy_costs_payback
        supply_side_df = supply_side_df[supply_side_df.index.get_level_values('vintage')>=initial_vintage]
        supply_side_df = supply_side_df[supply_side_df.index.get_level_values('year')>=initial_vintage]
        supply_side_df = supply_side_df.sort_index()
        demand_side_df = self.demand.d_annual_costs_payback
        demand_side_df.columns = ['value']
        demand_side_df = demand_side_df[demand_side_df.index.get_level_values('vintage')>=initial_vintage]
        demand_side_df = demand_side_df[demand_side_df.index.get_level_values('year')>=initial_vintage]
        demand_side_df = demand_side_df.reindex(supply_side_df.index).sort_index()
        sales_df = copy.deepcopy(self.demand.outputs.d_sales)
        util.replace_index_name(sales_df,'vintage','year')
        sales_df = sales_df[sales_df.index.get_level_values('vintage')>=initial_vintage]     
        sales_df = util.add_and_set_index(sales_df,'year',cfg.supply_years)
        sales_df.index = sales_df.index.reorder_levels(supply_side_df.index.names)
        sales_df = sales_df.reindex(supply_side_df.index).sort_index()
        keys = ['SUPPLY-SIDE', 'DEMAND-SIDE']
        names = ['COST TYPE']
        self.outputs.c_payback = pd.concat([util.DfOper.divi([supply_side_df, sales_df]), util.DfOper.divi([demand_side_df, sales_df])],keys=keys,names=names)
        self.outputs.c_payback = self.outputs.c_payback[np.isfinite(self.outputs.c_payback.values)]        
        self.outputs.c_payback = self.outputs.c_payback.replace([np.inf,np.nan],0)
        for sector in self.demand.sectors.values():
          for subsector in sector.subsectors.values():
                if hasattr(subsector,'stock') and subsector.sub_type!='link':
                    indexer = util.level_specific_indexer(self.outputs.c_payback,'subsector',subsector.id)
                    self.outputs.c_payback.loc[indexer,'unit'] = subsector.stock.unit.upper()
        self.outputs.c_payback = self.outputs.c_payback.set_index('unit', append=True)
        self.outputs.c_payback.columns = [cost_unit.upper()]
        self.outputs.c_payback['lifetime_year'] = self.outputs.c_payback.index.get_level_values('year')-self.outputs.c_payback.index.get_level_values('vintage')+1    
        self.outputs.c_payback = self.outputs.c_payback.set_index('lifetime_year',append=True)
        self.outputs.c_payback = util.remove_df_levels(self.outputs.c_payback,'year')
        self.outputs.c_payback = self.outputs.c_payback.groupby(level = [x for x in self.outputs.c_payback.index.names if x !='lifetime_year']).transform(lambda x: x.cumsum())
        self.outputs.c_payback = self.outputs.c_payback[self.outputs.c_payback[cost_unit.upper()]!=0]
        self.outputs.c_payback = self.outputs.return_cleaned_output('c_payback')
        
    def calculate_d_payback(self):
        cost_unit = cfg.cfgfile.get('case','currency_year_id') + " " + cfg.cfgfile.get('case','currency_name')
        initial_vintage = min(cfg.supply_years)
        demand_side_df = self.demand.d_annual_costs_payback
        demand_side_df.columns = ['value']
        demand_side_df = demand_side_df[demand_side_df.index.get_level_values('vintage')>=initial_vintage]
        demand_side_df = demand_side_df[demand_side_df.index.get_level_values('year')>=initial_vintage]
        sales_df = copy.deepcopy(self.demand.outputs.d_sales)
        util.replace_index_name(sales_df,'vintage','year')
        sales_df = sales_df[sales_df.index.get_level_values('vintage')>=initial_vintage]     
        sales_df = util.add_and_set_index(sales_df,'year',cfg.supply_years)
        sales_df.index = sales_df.index.reorder_levels(demand_side_df.index.names)
        sales_df = sales_df.reindex(demand_side_df.index).sort_index()
        self.demand.outputs.d_payback = util.DfOper.divi([demand_side_df, sales_df])
        self.demand.outputs.d_payback = self.demand.outputs.d_payback[np.isfinite(self.demand.outputs.d_payback.values)]        
        self.demand.outputs.d_payback = self.demand.outputs.d_payback.replace([np.inf,np.nan],0)
        for sector in self.demand.sectors.values():
          for subsector in sector.subsectors.values():
                if hasattr(subsector,'stock') and subsector.sub_type!='link':
                    indexer = util.level_specific_indexer(self.demand.outputs.d_payback,'subsector',subsector.id)
                    self.demand.outputs.d_payback.loc[indexer,'unit'] = subsector.stock.unit.upper()
        self.demand.outputs.d_payback = self.demand.outputs.d_payback.set_index('unit', append=True)
        self.demand.outputs.d_payback.columns = [cost_unit.upper()]
        self.demand.outputs.d_payback['lifetime_year'] = self.demand.outputs.d_payback.index.get_level_values('year')-self.demand.outputs.d_payback.index.get_level_values('vintage')+1    
        self.demand.outputs.d_payback = self.demand.outputs.d_payback.set_index('lifetime_year',append=True)
        self.demand.outputs.d_payback = util.remove_df_levels(self.demand.outputs.d_payback,'year')
        self.demand.outputs.d_payback = self.demand.outputs.d_payback.groupby(level = [x for x in self.demand.outputs.d_payback.index.names if x !='lifetime_year']).transform(lambda x: x.cumsum())
        self.demand.outputs.d_payback = self.demand.outputs.d_payback[self.demand.outputs.d_payback[cost_unit.upper()]!=0]
        self.demand.outputs.d_payback = self.demand.outputs.return_cleaned_output('d_payback')
   
    def calculate_d_payback_energy(self):
        initial_vintage = min(cfg.supply_years)
        demand_side_df = self.demand.d_all_energy_demand_payback
        demand_side_df.columns = ['value']
        demand_side_df = demand_side_df[demand_side_df.index.get_level_values('vintage')>=initial_vintage]
        demand_side_df = demand_side_df[demand_side_df.index.get_level_values('year')>=initial_vintage]
        sales_df = copy.deepcopy(self.demand.outputs.d_sales)
        util.replace_index_name(sales_df,'vintage','year')
        sales_df = sales_df[sales_df.index.get_level_values('vintage')>=initial_vintage]     
        sales_df = util.add_and_set_index(sales_df,'year',cfg.supply_years)
#        sales_df.index = sales_df.index.reorder_levels(demand_side_df.index.names)
#        sales_df = sales_df.reindex(demand_side_df.index).sort_index()
        self.demand.outputs.d_payback_energy = util.DfOper.divi([demand_side_df, sales_df])
        self.demand.outputs.d_payback_energy = self.demand.outputs.d_payback_energy[np.isfinite(self.demand.outputs.d_payback_energy.values)]        
        self.demand.outputs.d_payback_energy = self.demand.outputs.d_payback_energy.replace([np.inf,np.nan],0)
        for sector in self.demand.sectors.values():
          for subsector in sector.subsectors.values():
                if hasattr(subsector,'stock') and subsector.sub_type!='link':
                    indexer = util.level_specific_indexer(self.demand.outputs.d_payback_energy,'subsector',subsector.id)
                    self.demand.outputs.d_payback_energy.loc[indexer,'unit'] = subsector.stock.unit.upper()
        self.demand.outputs.d_payback_energy = self.demand.outputs.d_payback_energy.set_index('unit', append=True)
        self.demand.outputs.d_payback_energy.columns = [cfg.calculation_energy_unit.upper()]
        self.demand.outputs.d_payback_energy['lifetime_year'] = self.demand.outputs.d_payback_energy.index.get_level_values('year')-self.demand.outputs.d_payback_energy.index.get_level_values('vintage')+1    
        self.demand.outputs.d_payback_energy = self.demand.outputs.d_payback_energy.set_index('lifetime_year',append=True)
        self.demand.outputs.d_payback_energy = util.remove_df_levels(self.demand.outputs.d_payback_energy,'year')
        self.demand.outputs.d_payback_energy = self.demand.outputs.d_payback_energy.groupby(level = [x for x in self.demand.outputs.d_payback_energy.index.names if x !='lifetime_year']).transform(lambda x: x.cumsum())
        self.demand.outputs.d_payback_energy = self.demand.outputs.d_payback_energy[self.demand.outputs.d_payback_energy[cfg.calculation_energy_unit.upper()]!=0]
        self.demand.outputs.d_payback_energy = self.demand.outputs.return_cleaned_output('d_payback_energy')

    def calc_and_format_export_costs(self):
        #calculate and format export costs
        if self.supply.export_costs is None:
            return None
        export_costs = cfg.geo.geo_map(self.supply.export_costs.copy(), cfg.supply_primary_geography, cfg.combined_outputs_geography, 'total')
        export_costs = Output.clean_df(export_costs)
        util.replace_index_name(export_costs, 'FINAL_ENERGY', 'SUPPLY_NODE_EXPORT')
        export_costs = util.add_to_df_index(export_costs, names=['EXPORT/DOMESTIC', "SUPPLY/DEMAND"], keys=["EXPORT", "SUPPLY"])
        cost_unit = cfg.cfgfile.get('case','currency_year_id') + " " + cfg.cfgfile.get('case','currency_name')
        export_costs.columns = [cost_unit.upper()]
        return export_costs

    def calc_and_format_embodied_costs(self):
        #calculate and format emobodied supply costs
        embodied_costs = Output.clean_df(self.demand.outputs.demand_embodied_energy_costs)
        cost_unit = cfg.cfgfile.get('case','currency_year_id') + " " + cfg.cfgfile.get('case','currency_name')
        embodied_costs.columns = [cost_unit.upper()]
        embodied_costs = util.add_to_df_index(embodied_costs, names=['EXPORT/DOMESTIC', "SUPPLY/DEMAND"], keys=["DOMESTIC","SUPPLY"])
        return embodied_costs

    def calc_and_format_direct_demand_costs(self):
        #calculte and format direct demand costs
        if self.demand.outputs.d_levelized_costs is None:
            return None
        direct_costs = cfg.geo.geo_map(self.demand.outputs.d_levelized_costs.copy(), cfg.demand_primary_geography, cfg.combined_outputs_geography, 'total')
        levels_to_keep = [x for x in cfg.output_combined_levels if x in direct_costs.index.names]
        direct_costs = direct_costs.groupby(level=levels_to_keep).sum()
        direct_costs = Output.clean_df(direct_costs)
        direct_costs = util.add_to_df_index(direct_costs, names=['EXPORT/DOMESTIC', "SUPPLY/DEMAND"], keys=["DOMESTIC","DEMAND"])
        return direct_costs

    def calculate_combined_cost_results(self):
        cost_unit = cfg.cfgfile.get('case','currency_year_id') + " " + cfg.cfgfile.get('case','currency_name')
        export_costs = self.calc_and_format_export_costs()
        embodied_costs = self.calc_and_format_embodied_costs()
        direct_costs = self.calc_and_format_direct_demand_costs()
        keys = ['EXPORTED', 'SUPPLY-SIDE', 'DEMAND-SIDE']
        names = ['COST TYPE']
        self.outputs.c_costs = util.df_list_concatenate([export_costs, embodied_costs, direct_costs],keys=keys,new_names=names)
        self.outputs.c_costs[self.outputs.c_costs<0]=0
        self.outputs.c_costs= self.outputs.c_costs[self.outputs.c_costs[cost_unit.upper()]!=0]

    def calc_and_format_export_emissions(self):
        #calculate and format export emissions
        if self.supply.export_emissions is None:
            return None
        export_emissions = cfg.geo.geo_map(self.supply.export_emissions.copy(), cfg.supply_primary_geography, cfg.combined_outputs_geography, 'total')
        if 'supply_geography' not in cfg.output_combined_levels:
            util.remove_df_levels(export_emissions, cfg.supply_primary_geography +'_supply')
        export_emissions = Output.clean_df(export_emissions)
        util.replace_index_name(export_emissions, 'FINAL_ENERGY','SUPPLY_NODE_EXPORT')
        export_emissions = util.add_to_df_index(export_emissions, names=['EXPORT/DOMESTIC', "SUPPLY/DEMAND"], keys=["EXPORT", "SUPPLY"])
        return export_emissions

    def calc_and_format_embodied_supply_emissions(self):
        # calculate and format embodied supply emissions
        embodied_emissions = Output.clean_df(self.demand.outputs.demand_embodied_emissions)
        embodied_emissions = util.add_to_df_index(embodied_emissions, names=['EXPORT/DOMESTIC', "SUPPLY/DEMAND"], keys=["DOMESTIC", "SUPPLY"])
        return embodied_emissions

    def calc_and_format_direct_demand_emissions(self):
        #calculte and format direct demand emissions
        direct_emissions = Output.clean_df(self.demand.outputs.demand_direct_emissions)
        direct_emissions = util.add_to_df_index(direct_emissions, names=['EXPORT/DOMESTIC', "SUPPLY/DEMAND"], keys=["DOMESTIC", "DEMAND"])
        if cfg.combined_outputs_geography + '_supply' in cfg.output_combined_levels:
             keys = direct_emissions.index.get_level_values(cfg.combined_outputs_geography.upper()).values
             names = cfg.combined_outputs_geography.upper() + '_SUPPLY'
             direct_emissions[names] = keys
             direct_emissions.set_index(names, append=True, inplace=True)
        return direct_emissions

    def calculate_combined_emissions_results(self):
        export_emissions = self.calc_and_format_export_emissions()
        embodied_emissions = self.calc_and_format_embodied_supply_emissions()
        direct_emissions = self.calc_and_format_direct_demand_emissions()
        keys = ['EXPORTED', 'SUPPLY-SIDE', 'DEMAND-SIDE']
        names = ['EMISSIONS TYPE']
        self.outputs.c_emissions = util.df_list_concatenate([export_emissions, embodied_emissions, direct_emissions],keys=keys,new_names = names)
        util.replace_index_name(self.outputs.c_emissions, cfg.combined_outputs_geography.upper() +'-EMITTED', cfg.combined_outputs_geography.upper() +'_SUPPLY')
        util.replace_index_name(self.outputs.c_emissions, cfg.combined_outputs_geography.upper() +'-CONSUMED', cfg.combined_outputs_geography.upper())
        self.outputs.c_emissions= self.outputs.c_emissions[self.outputs.c_emissions['VALUE']!=0]
        emissions_unit = cfg.cfgfile.get('case','mass_unit')
        self.outputs.c_emissions.columns = [emissions_unit.upper()]

    def calc_and_format_export_energy(self):
        if self.supply.export_energy is None:
            return None
        export_energy = cfg.geo.geo_map(self.supply.export_energy.copy(), cfg.supply_primary_geography, cfg.combined_outputs_geography, 'total')
        export_energy = Output.clean_df(export_energy)
        util.replace_index_name(export_energy, 'FINAL_ENERGY','SUPPLY_NODE_EXPORT')
        export_energy = util.add_to_df_index(export_energy, names=['EXPORT/DOMESTIC', "ENERGY ACCOUNTING"], keys=["EXPORT", "EMBODIED"])
        return export_energy

    def calc_and_format_embodied_supply_energy(self):
        embodied_energy = Output.clean_df(self.demand.outputs.demand_embodied_energy)
        embodied_energy = embodied_energy[embodied_energy['VALUE']!=0]
        embodied_energy = util.add_to_df_index(embodied_energy, names=['EXPORT/DOMESTIC', "ENERGY ACCOUNTING"], keys=['DOMESTIC','EMBODIED'])
        return embodied_energy

    def calc_and_format_direct_demand_energy(self):
        demand_energy = cfg.geo.geo_map(self.demand.outputs.d_energy.copy(), cfg.demand_primary_geography, cfg.combined_outputs_geography, 'total')
        demand_energy = Output.clean_df(demand_energy)
        demand_energy = demand_energy[demand_energy.index.get_level_values('YEAR')>=int(cfg.cfgfile.get('case','current_year'))]
        demand_energy = util.add_to_df_index(demand_energy, names=['EXPORT/DOMESTIC', "ENERGY ACCOUNTING"], keys=['DOMESTIC','FINAL'])
        return demand_energy

    def calculate_combined_energy_results(self):
        export_energy = self.calc_and_format_export_energy()
        embodied_energy = self.calc_and_format_embodied_supply_energy()
        demand_energy = self.calc_and_format_direct_demand_energy()

        # reorder levels so dfs match
        for name in [x for x in embodied_energy.index.names if x not in demand_energy.index.names]:
            demand_energy[name] = "N/A"
            demand_energy.set_index(name,append=True,inplace=True)
        demand_energy = demand_energy.groupby(level=embodied_energy.index.names).sum()
        demand_energy = demand_energy.reorder_levels(embodied_energy.index.names)
        if export_energy is not None:
            for name in [x for x in embodied_energy.index.names if x not in export_energy.index.names]:
                export_energy[name] = "N/A"
                export_energy.set_index(name,append=True,inplace=True)
            export_energy = export_energy.groupby(level=embodied_energy.index.names).sum()
            export_energy = export_energy.reorder_levels(embodied_energy.index.names)

        self.outputs.c_energy = pd.concat([embodied_energy, demand_energy, export_energy])
        self.outputs.c_energy = self.outputs.c_energy[self.outputs.c_energy['VALUE']!=0]
        energy_unit = cfg.calculation_energy_unit
        self.outputs.c_energy.columns = [energy_unit.upper()]

    def export_io(self):
        io_table_write_step = int(cfg.cfgfile.get('output_detail','io_table_write_step'))
        io_table_years = sorted([min(cfg.supply_years)] + range(max(cfg.supply_years), min(cfg.supply_years), -io_table_write_step))
        df_list = []
        for year in io_table_years:
            sector_df_list = []
            keys = self.supply.demand_sectors
            name = ['sector']
            for sector in self.supply.demand_sectors:
                sector_df_list.append(self.supply.io_dict[year][sector])
            year_df = pd.concat(sector_df_list, keys=keys,names=name)
            year_df = pd.concat([year_df]*len(keys),keys=keys,names=name,axis=1)
            df_list.append(year_df)
        keys = io_table_years
        name = ['year']
        df = pd.concat(df_list,keys=keys,names=name)
        for row_sector in self.supply.demand_sectors:
            for col_sector in self.supply.demand_sectors:
                if row_sector != col_sector:
                    df.loc[util.level_specific_indexer(df,'sector',row_sector),util.level_specific_indexer(df,'sector',col_sector,axis=1)] = 0
        self.supply.outputs.io = df
        result_df = self.supply.outputs.return_cleaned_output('io')
        keys = [self.scenario.name.upper(), cfg.timestamp]
        names = ['SCENARIO','TIMESTAMP']
        for key, name in zip(keys,names):
            result_df = pd.concat([result_df], keys=[key],names=[name])
        Output.write(result_df, 's_io.csv', os.path.join(cfg.workingdir, 'supply_outputs'))
#        self.export_stacked_io()

    def export_stacked_io(self):
        df = copy.deepcopy(self.supply.outputs.io)
        df.index.names = [x + '_input'if x!= 'year' else x for x in df.index.names ]
        df = df.stack(level=df.columns.names).to_frame()
        df.columns = ['value']
        self.supply.outputs.stacked_io = df
        result_df = self.supply.outputs.return_cleaned_output('stacked_io')
        keys = [self.scenario.name.upper(), cfg.timestamp]
        names = ['SCENARIO','TIMESTAMP']
        for key, name in zip(keys,names):
            result_df = pd.concat([result_df], keys=[key],names=[name])
        Output.write(result_df, 's_stacked_io.csv', os.path.join(cfg.workingdir, 'supply_outputs'))
