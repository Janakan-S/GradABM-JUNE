import torch
import pickle
import numpy as np
import pandas as pd
from torch.utils.checkpoint import checkpoint
import yaml
import pyro
from pathlib import Path

from torch_june.paths import default_config_path
from torch_june import TorchJune, Timer, TransmissionSampler
from torch_june.utils import read_path
from torch_june.infection_seed import infect_fraction_of_people


class Runner(torch.nn.Module):
    def __init__(
        self,
        model,
        data,
        timer,
        log_fraction_initial_cases,
        save_path,
        parameters,
        age_bins=(0, 18, 25, 65, 80, 100),
    ):
        super().__init__()
        self.model = model
        self.data = data
        self.data_backup = self.backup_infection_data(data)
        self.timer = timer
        self.log_fraction_initial_cases = log_fraction_initial_cases
        self.device = model.device
        self.age_bins = torch.tensor(age_bins, device=self.device)
        self.ethnicities = np.sort(np.unique(data["agent"].ethnicity))
        self.n_agents = data["agent"].id.shape[0]
        self.population_by_age = self.get_people_by_age()
        self.save_path = Path(save_path)
        self.input_parameters = parameters
        self.restore_initial_data()

    @classmethod
    def from_file(cls, fpath=default_config_path):
        with open(fpath, "r") as f:
            params = yaml.safe_load(f)
        return cls.from_parameters(params)

    @classmethod
    def from_parameters(cls, params):
        model = TorchJune.from_parameters(params)
        data = cls.get_data(params)
        timer = Timer.from_parameters(params)
        return cls(
            model=model,
            data=data,
            timer=timer,
            log_fraction_initial_cases=params["infection_seed"][
                "log_fraction_initial_cases"
            ],
            save_path=params["save_path"],
            parameters=params,
        )

    @staticmethod
    def get_data(params):
        device = params["system"]["device"]
        data_path = read_path(params["data_path"])
        with open(data_path, "rb") as f:
            data = pickle.load(f).to(device)
        n_agents = len(data["agent"]["id"])
        inf_params = {}
        transmission_sampler = TransmissionSampler.from_parameters(params)
        transmission_values = transmission_sampler(n_agents)
        inf_params["max_infectiousness"] = transmission_values[0, :]
        inf_params["shape"] = transmission_values[1, :]
        inf_params["rate"] = transmission_values[2, :]
        inf_params["shift"] = transmission_values[3, :]
        data["agent"].infection_parameters = inf_params
        data["agent"].transmission = torch.zeros(n_agents, device=device)
        data["agent"].susceptibility = torch.ones(n_agents, device=device)
        data["agent"].is_infected = torch.zeros(n_agents, device=device)
        data["agent"].infection_time = torch.zeros(n_agents, device=device)
        symptoms = {}
        symptoms["current_stage"] = torch.ones(
            n_agents, dtype=torch.long, device=device
        )
        symptoms["next_stage"] = torch.ones(n_agents, dtype=torch.long, device=device)
        symptoms["time_to_next_stage"] = torch.zeros(n_agents, device=device)
        data["agent"].symptoms = symptoms
        return data

    def backup_infection_data(self, data):
        ret = {}
        ret["susceptibility"] = data["agent"].susceptibility.detach().clone()
        ret["is_infected"] = data["agent"].is_infected.detach().clone()
        ret["infection_time"] = data["agent"].infection_time.detach().clone()
        ret["transmission"] = data["agent"].transmission.detach().clone()
        symptoms = {}
        symptoms["current_stage"] = (
            data["agent"]["symptoms"]["current_stage"].detach().clone()
        )
        symptoms["next_stage"] = (
            data["agent"]["symptoms"]["next_stage"].detach().clone()
        )
        symptoms["time_to_next_stage"] = (
            data["agent"]["symptoms"]["time_to_next_stage"].detach().clone()
        )
        ret["symptoms"] = symptoms
        return ret

    def restore_initial_data(self):
        self.data["agent"].transmission = (
            self.data_backup["transmission"].detach().clone()
        )
        self.data["agent"].susceptibility = (
            self.data_backup["susceptibility"].detach().clone()
        )
        self.data["agent"].is_infected = (
            self.data_backup["is_infected"].detach().clone()
        )
        self.data["agent"].infection_time = (
            self.data_backup["infection_time"].detach().clone()
        )
        self.data["agent"].symptoms["current_stage"] = (
            self.data_backup["symptoms"]["current_stage"].detach().clone()
        )
        self.data["agent"].symptoms["next_stage"] = (
            self.data_backup["symptoms"]["next_stage"].detach().clone()
        )
        self.data["agent"].symptoms["time_to_next_stage"] = (
            self.data_backup["symptoms"]["time_to_next_stage"].detach().clone()
        )
        # reset results
        self.data["results"] = {}
        self.data["results"]["daily_deaths"] = None
        self.data["results"]["daily_deaths_by_district"] = None

    def set_initial_cases(self):
        fraction_initial_cases = 10.0**self.log_fraction_initial_cases
        new_infected = infect_fraction_of_people(
            data=self.data,
            timer=self.timer,
            symptoms_updater=self.model.symptoms_updater,
            device=self.device,
            fraction=fraction_initial_cases,
        )
        self.model.symptoms_updater(
            data=self.data, timer=self.timer, new_infected=new_infected
        )

    def forward(self):
        timer = self.timer
        model = self.model
        data = self.data
        timer.reset()
        self.restore_initial_data()
        self.set_initial_cases()
        # data = model(data, timer)
        cases_per_timestep = data["agent"].is_infected.sum()
        #cases_by_age = self.get_cases_by_age(data)
        #cases_by_ethnicity = self.get_cases_by_ethnicity(data)
        self.store_differentiable_deaths(data)
        deaths_per_timestep = self.get_deaths_from_symptoms(data["agent"].symptoms)
        dates = [timer.date]
        i = 0
        while timer.date < timer.final_date:
            i += 1
            next(timer)
            data = model(data, timer)
            cases = data["agent"].is_infected.sum()
            cases_per_timestep = torch.hstack((cases_per_timestep, cases))
            deaths = self.get_deaths_from_symptoms(data["agent"].symptoms)
            self.store_differentiable_deaths(data)
            deaths_per_timestep = torch.hstack((deaths_per_timestep, deaths))
            #cases_age = self.get_cases_by_age(data)
            #cases_by_age = torch.vstack((cases_by_age, cases_age))
            #cases_ethnicity = self.get_cases_by_ethnicity(data)
            #cases_by_ethnicity = torch.vstack((cases_by_ethnicity, cases_ethnicity))
            dates.append(timer.date)
        results = {
            "dates": dates,
            "cases_per_timestep": cases_per_timestep,
            "daily_cases_per_timestep": torch.diff(
                cases_per_timestep, prepend=torch.tensor([0.0], device=self.device)
            ),
            "deaths_per_timestep": deaths_per_timestep,
            "daily_deaths_by_district": data["results"]["daily_deaths_by_district"],
        }
        #for (i, key) in enumerate(self.age_bins[1:]):
        #    results[f"cases_by_age_{key:02d}"] = cases_by_age[:, i]
        #for (i, key) in enumerate(self.ethnicities):
        #    results[f"cases_by_ethnicity_{key}"] = cases_by_ethnicity[:, i]
        return results, data["agent"].is_infected

    def save_results(self, results, is_infected):
        self.save_path.mkdir(exist_ok=True, parents=True)
        df = pd.DataFrame(index=results["dates"])
        df.index.name = "date"
        for key in results:
            if key in ("dates", "daily_deaths_by_district"):
                continue
            df[key] = results[key].detach().cpu().numpy()
        df.to_csv(self.save_path / "results.csv")
        df = pd.DataFrame()
        df["is_infected"] = is_infected
        df.to_csv(self.save_path / "results_is_infected.csv")

    def get_deaths_from_symptoms(self, symptoms):
        return torch.tensor(
            symptoms["current_stage"][
                symptoms["current_stage"] == self.model.symptoms_updater.stages_ids[-1]
            ].shape[0],
            device=self.device,
        )

    def get_deaths_by_district(self, symptoms):
        if "district" in self.data["agent"]:
            districts = self.data["agent"].district.unique()
            dead_idcs = self.data["agent"].district[
                symptoms["current_stage"] == self.model.symptoms_updater.stages_ids[-1]
            ]
            dead_districts = self.data["agent"].district[dead_idcs]
            ret = torch.zeros(districts.shape, dtype=torch.long, device=self.device)
            deaths, counts = torch.unique(dead_districts, return_counts=True)
            ret[deaths] = counts
            return ret
        else:
            return torch.zeros(1, 1)

    def store_differentiable_deaths(self, data):
        """
        Returns differentiable deaths by district and global. The results are stored
        in data["results"]
        """
        symptoms = data["agent"].symptoms
        dead_idx = self.model.symptoms_updater.stages_ids[-1]
        deaths = (
            (symptoms["current_stage"] == dead_idx)
            * symptoms["current_stage"]
            / dead_idx
        )
        if "district" in data["agent"]:
            districts, _ = torch.sort(data["agent"].district.unique())
            deaths_by_district = []
            for i, district in enumerate(districts):
                mask_district = data["agent"].district == district
                deaths_district = (deaths * mask_district).sum()
                deaths_by_district.append(deaths_district.reshape(1))
            deaths_by_district = torch.cat(deaths_by_district, 0)
            if data["results"]["daily_deaths_by_district"] is not None:
                data["results"]["daily_deaths_by_district"] = torch.vstack(
                    (data["results"]["daily_deaths_by_district"], deaths_by_district)
                )
            else:
                data["results"]["daily_deaths_by_district"] = deaths_by_district
        if data["results"]["daily_deaths"] is not None:
            data["results"]["daily_deaths"] = torch.hstack(
                (data["results"]["daily_deaths"], deaths.sum())
            )
        else:
            data["results"]["daily_deaths"] = deaths.sum()

    def get_cases_by_age(self, data):
        ret = torch.zeros(self.age_bins.shape[0] - 1, device=self.device)
        for i in range(1, self.age_bins.shape[0]):
            mask1 = data["agent"].age < self.age_bins[i]
            mask2 = data["agent"].age > self.age_bins[i - 1]
            mask = mask1 * mask2
            ret[i - 1] = (data["agent"].is_infected * mask).sum()
        return ret

    def get_people_by_age(self):
        ret = torch.zeros(self.age_bins.shape[0] - 1, device=self.device)
        for i in range(1, self.age_bins.shape[0]):
            mask1 = self.data["agent"].age < self.age_bins[i]
            mask2 = self.data["agent"].age > self.age_bins[i - 1]
            mask = mask1 * mask2
            ret[i - 1] = mask.sum()
        return ret

    def get_cases_by_ethnicity(self, data):
        ret = torch.zeros(len(self.ethnicities), device=self.device)
        for i, ethnicity in enumerate(self.ethnicities):
            mask = torch.tensor(
                self.data["agent"].ethnicity == ethnicity, device=self.device
            )
            ret[i] = (mask * data["agent"].is_infected).sum()
        return ret
