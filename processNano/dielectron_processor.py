import sys

sys.path.append("copperhead/")

import awkward
import awkward as ak
import numpy as np

import pandas as pd
import coffea.processor as processor
#from coffea.lookup_tools import extractor
from coffea.lumi_tools import LumiMask
from processNano.timer import Timer
from processNano.weights import Weights

from config.parameters import parameters, ele_branches, jet_branches
from copperhead.stage1.corrections.pu_reweight import pu_lookups, pu_evaluator
from copperhead.stage1.corrections.l1prefiring_weights import l1pf_weights
from processNano.electrons import find_dielectron, fill_electrons
from processNano.jets import prepare_jets, fill_jets, fill_bjets, btagSF

import copy
from processNano.corrections.kFac import kFac
from copperhead.stage1.corrections.jec import jec_factories, apply_jec


class DielectronProcessor(processor.ProcessorABC):
    def __init__(self, **kwargs):
        self.samp_info = kwargs.pop("samp_info", None)
        do_timer = kwargs.pop("do_timer", True)
        self.apply_to_output = kwargs.pop("apply_to_output", None)
        self.pt_variations = kwargs.pop("pt_variations", ["nominal"])

        self.year = self.samp_info.year
        self.parameters = {k: v[self.year] for k, v in parameters.items()}

        self.do_btag = True

        if self.samp_info is None:
            print("Samples info missing!")
            return

        self._accumulator = processor.defaultdict_accumulator(int)

        self.applykFac = True
        self.applyNNPDFWeight = True
        self.do_pu = True
        self.auto_pu = False
        self.do_l1pw = True  # L1 prefiring weights
        self.do_jecunc = True
        self.do_jerunc = False
        
        self.timer = Timer("global") if do_timer else None
        
        self._columns = self.parameters["proc_columns"]

        self.regions = ["bb", "be"]
        self.channels = ["mumu"]

        self.lumi_weights = self.samp_info.lumi_weights
        
        self.prepare_lookups()


    @property
    def accumulator(self):
        return self._accumulator

    @property
    def columns(self):
        return self._columns

    def process(self, df):

        # Initialize timer
        if self.timer:
            self.timer.update()

        # Dataset name (see definitions in config/datasets.py)
        dataset = df.metadata["dataset"]

        is_mc = True
        if "data" in dataset:
            is_mc = False

        # ------------------------------------------------------------#
        # Apply HLT, lumimask, genweights, PU weights
        # and L1 prefiring weights
        # ------------------------------------------------------------#

        numevents = len(df)

        # All variables that we want to save
        # will be collected into the 'output' dataframe
        output = pd.DataFrame(
            {"run": df.run, "event": df.event, "luminosityBlock": df.luminosityBlock}
        )
        output.index.name = "entry"
        output["npv"] = df.PV.npvs
        output["met"] = df.MET.pt

        # Separate dataframe to keep track on weights
        # and their systematic variations
        weights = Weights(output)
        ele_branches_local = copy.copy(ele_branches)

        if is_mc:
            genPart = df.GenPart
            genPart = genPart[
                (
                    (abs(genPart.pdgId) == 11) | abs(genPart.pdgId)
                    == 13 | (abs(genPart.pdgId) == 15)
                )
                & genPart.hasFlags(["isHardProcess", "fromHardProcess", "isPrompt"])
            ]

            cut = ak.num(genPart) == 2
            output["dielectron_mass_gen"] = cut
            output["dielectron_pt_gen"] = cut
            output["dielectron_eta_gen"] = cut
            output["dielectron_phi_gen"] = cut
            genMother = genPart[cut][:, 0] + genPart[cut][:, 1]
            output.loc[
                output["dielectron_mass_gen"] == True, ["dielectron_mass_gen"]
            ] = genMother.mass
            output.loc[
                output["dielectron_pt_gen"] == True, ["dielectron_pt_gen"]
            ] = genMother.pt
            output.loc[
                output["dielectron_eta_gen"] == True, ["dielectron_eta_gen"]
            ] = genMother.eta
            output.loc[
                output["dielectron_phi_gen"] == True, ["dielectron_phi_gen"]
            ] = genMother.phi
            output.loc[output["dielectron_mass_gen"] == False, ["dielectron_mass_gen"]] = -999.0
            output.loc[output["dielectron_pt_gen"] == False, ["dielectron_pt_gen"]] = -999.0
            output.loc[output["dielectron_eta_gen"] == False, ["dielectron_eta_gen"]] = -999.0
            output.loc[output["dielectron_phi_gen"] == False, ["dielectron_phi_gen"]] = -999.0

        else:
            output["dielectron_mass_gen"] = -999.0
            output["dielectron_pt_gen"] = -999.0
            output["dielectron_eta_gen"] = -999.0
            output["dielectron_phi_gen"] = -999.0

        output["dielectron_mass_gen"] = output["dielectron_mass_gen"].astype(float)
        output["dielectron_pt_gen"] = output["dielectron_pt_gen"].astype(float)
        output["dielectron_eta_gen"] = output["dielectron_eta_gen"].astype(float)
        output["dielectron_phi_gen"] = output["dielectron_phi_gen"].astype(float)

        if is_mc:
            # For MC: Apply gen.weights, pileup weights, lumi weights,
            # L1 prefiring weights
            mask = np.ones(numevents, dtype=bool)
            genweight = df.genWeight
            weights.add_weight("genwgt", genweight)
            weights.add_weight("lumi", self.lumi_weights[dataset])
            if self.do_pu:
                pu_wgts = pu_evaluator(
                    self.pu_lookups,
                    self.parameters,
                    numevents,
                    np.array(df.Pileup.nTrueInt),
                    self.auto_pu,
                )
                weights.add_weight("pu_wgt", pu_wgts, how="all")
            if self.do_l1pw:
                if self.parameters["do_l1prefiring_wgts"]:
                    if "L1PreFiringWeight" in df.fields:
                        l1pfw = l1pf_weights(df)
                        weights.add_weight("l1prefiring_wgt", l1pfw, how="all")
                    else:
                        weights.add_weight("l1prefiring_wgt", how="dummy_vars")

            df["Electron", "pt_gen"] = df.Electron.matched_gen.pt
            df["Electron", "eta_gen"] = df.Electron.matched_gen.eta
            df["Electron", "phi_gen"] = df.Electron.matched_gen.phi
            df["Electron", "idx"] = df.Electron.genPartIdx
            ele_branches_local += ["genPartFlav", "pt_gen", "eta_gen", "phi_gen", "idx"]
        else:
            # For Data: apply Lumi mask
            lumi_info = LumiMask(self.parameters["lumimask_UL_el"])
            mask = lumi_info(df.run, df.luminosityBlock)

        # Apply HLT to both Data and MC
        hlt = ak.to_pandas(df.HLT[self.parameters["el_hlt"]])
        hlt = hlt[self.parameters["el_hlt"]].sum(axis=1)

        if self.timer:
            self.timer.add_checkpoint("Applied HLT and lumimask")

        # Save raw variables before computing any corrections

        df["Electron", "pt_raw"] = df.Electron.pt
        df["Electron", "eta_raw"] = df.Electron.eta
        df["Electron", "phi_raw"] = df.Electron.phi

        if True:  # indent reserved for loop over pT variations

            # --- conversion from awkward to pandas --- #
            electrons = ak.to_pandas(df.Electron[ele_branches_local])
            electrons.pt = electrons.pt_raw * (electrons.scEtOverPt + 1.0)
            electrons.eta = electrons.eta_raw + electrons.deltaEtaSC
            electrons = electrons.dropna()
            electrons = electrons.loc[:, ~electrons.columns.duplicated()]
            if is_mc:
                electrons.loc[electrons.idx == -1, "pt_gen"] = -999.0
                electrons.loc[electrons.idx == -1, "eta_gen"] = -999.0
                electrons.loc[electrons.idx == -1, "phi_gen"] = -999.0

            if self.timer:
                self.timer.add_checkpoint("load electron data")

            # --------------------------------------------------------#
            # Electron selection
            # --------------------------------------------------------#

            # Apply event quality flag
            flags = ak.to_pandas(df.Flag)
            flags = flags[self.parameters["event_flags"]].product(axis=1)

            # Define baseline muon selection (applied to pandas DF!)
            electrons["selection"] = (
                (electrons.pt > self.parameters["electron_pt_cut"])
                & (abs(electrons.eta) < self.parameters["electron_eta_cut"])
                & (electrons[self.parameters["electron_id"]] > 0)
            )

            if dataset == "dyInclusive50":
                electrons = electrons[electrons.genPartFlav == 15]
            # Count electrons
            nelectrons = (
                electrons[electrons.selection]
                .reset_index()
                .groupby("entry")["subentry"]
                .nunique()
            )
            if is_mc:
                output["event_selection"] = mask & (hlt > 0) & (nelectrons >= 2)
                output["event_selection"] = mask & (hlt > 0) & (nelectrons >= 2)
            else:
                output["event_selection"] = mask & (hlt > 0) & (nelectrons >= 4)
                output["event_selection"] = mask & (hlt > 0) & (nelectrons >= 4)
                
            if self.timer:
                self.timer.add_checkpoint("Selected events and electrons")

            # --------------------------------------------------------#
            # Initialize electron variables
            # --------------------------------------------------------#

            if is_mc:
                electrons = electrons[electrons.selection & (nelectrons >= 2)]
            else:
                electrons = electrons[electrons.selection & (nelectrons >= 4)]

            if self.timer:
                self.timer.add_checkpoint("electron object selection")

            output["r"] = None
            output["dataset"] = dataset
            output["year"] = int(self.year)

            if electrons.shape[0] == 0:
                output = output.reindex(sorted(output.columns), axis=1)
                output = output[output.r.isin(self.regions)]
                if self.apply_to_output is None:
                    return output
                else:
                    self.apply_to_output(output)
                    return self.accumulator.identity()

            result = electrons.groupby("entry").apply(find_dielectron, is_mc=is_mc)
            if is_mc:
                dielectron = pd.DataFrame(
                    result.to_list(), columns=["idx1", "idx2", "mass"]
                )
            else:
                dielectron = pd.DataFrame(
                    result.to_list(), columns=["idx1", "idx2", "mass"]
                )
            e1 = electrons.loc[dielectron.idx1.values, :]
            e2 = electrons.loc[dielectron.idx2.values, :]
            e1.index = e1.index.droplevel("subentry")
            e2.index = e2.index.droplevel("subentry")
            if self.timer:
                self.timer.add_checkpoint("dielectron pair selection")

            if self.timer:
                self.timer.add_checkpoint("back back angle calculation")
            dielectron_mass = dielectron.mass

            # --------------------------------------------------------#
            # Select events with muons passing leading pT cut
            # and trigger matching
            # --------------------------------------------------------#

            # Events where there is at least one muon passing
            # leading muon pT cut
            # if self.timer:
            #    self.timer.add_checkpoint("Applied trigger matching")

            # --------------------------------------------------------#
            # Fill dielectron and electron variables
            # --------------------------------------------------------#

            fill_electrons(output, e1, e2, is_mc)

            if self.timer:
                self.timer.add_checkpoint("all electron variables")

        # ------------------------------------------------------------#
        # Prepare jets
        # ------------------------------------------------------------#

        prepare_jets(df, is_mc)

        # ------------------------------------------------------------#
        # Apply JEC, get JEC and JER variations
        # ------------------------------------------------------------#

        jets = df.Jet

        self.do_jec = False

        # We only need to reapply JEC for 2018 data
        # (unless new versions of JEC are released)
        if ("data" in dataset) and ("2018" in self.year):
            self.do_jec = False

        apply_jec(
            df,
            jets,
            dataset,
            is_mc,
            self.year,
            self.do_jec,
            self.do_jecunc,
            self.do_jerunc,
            self.jec_factories,
            self.jec_factories_data,
        )
        output.columns = pd.MultiIndex.from_product(
            [output.columns, [""]], names=["Variable", "Variation"]
        )

        if self.timer:
            self.timer.add_checkpoint("Jet preparation & event weights")

        for v_name in self.pt_variations:
            output_updated = self.jet_loop(
                v_name,
                is_mc,
                df,
                dataset,
                mask,
                electrons,
                e1,
                e2,
                jets,
                jet_branches,
                weights,
                numevents,
                output,
            )
            if output_updated is not None:
                output = output_updated

        if self.timer:
            self.timer.add_checkpoint("Jet loop")
        # ------------------------------------------------------------#
        # Calculate other event weights
        # ------------------------------------------------------------#

        """
        if is_mc:
            do_zpt = ('dy' in dataset)
            if do_zpt:
                zpt_weight = np.ones(numevents, dtype=float)
                zpt_weight[two_muons] =\
                    self.evaluator[self.zpt_path](
                        output['dimuon_pt'][two_muons]
                    ).flatten()
                weights.add_weight('zpt_wgt', zpt_weight)

        if self.timer:
            self.timer.add_checkpoint("Computed event weights")
        """

        # ------------------------------------------------------------#
        # Fill outputs
        # ------------------------------------------------------------#
        # mass = output.dielectron_mass
        output["r"] = None
        output.loc[
            ((abs(output.e1_eta) < 1.442) & (abs(output.e2_eta) < 1.442)), "r"
        ] = "bb"
        output.loc[
            ((abs(output.e1_eta) > 1.566) ^ (abs(output.e2_eta) > 1.566)), "r"
        ] = "be"
        output.loc[
            ((abs(output.e1_eta) > 1.566) & (abs(output.e2_eta) > 1.566)), "r"
        ] = "ee"

        for wgt in weights.df.columns:

            if wgt == "pu_wgt_off":
                output["pu_wgt"] = weights.get_weight(wgt)
            if wgt != "nominal":
                output[f"wgt_{wgt}"] = weights.get_weight(wgt)


        if is_mc and "dy" in dataset:
            mass_bb = output[output["r"] == "bb"].dielectron_mass_gen.to_numpy()
            mass_be = output[output["r"] == "be"].dielectron_mass_gen.to_numpy()
            for key in output.columns:
                if "wgt" not in key[0]:
                    continue
                output.loc[
                    ((abs(output.e1_eta) < 1.2) & (abs(output.e2_eta) < 1.2)),
                    key[0],
                ] = (
                    output.loc[
                        ((abs(output.e1_eta) < 1.2) & (abs(output.e2_eta) < 1.2)),
                        key[0],
                    ]
                    * kFac(mass_bb, "bb", "el")
                ).values
                output.loc[
                    ((abs(output.e1_eta) > 1.2) | (abs(output.e2_eta) > 1.2)),
                    key[0],
                ] = (
                    output.loc[
                        ((abs(output.e1_eta) > 1.2) | (abs(output.e2_eta) > 1.2)),
                        key[0],
                    ]
                    * kFac(mass_be, "be", "el")
                ).values

        if is_mc and "dy" in dataset and self.applyNNPDFWeight:
            mass_bb = output[output["r"] == "bb"].dielectron_mass_gen.to_numpy()
            mass_be = output[output["r"] == "be"].dielectron_mass_gen.to_numpy()
            leadingPt_bb = output[output["r"] == "bb"].e1_pt_gen.to_numpy()
            leadingPt_be = output[output["r"] == "be"].e1_pt_gen.to_numpy()
            for key in output.columns:
                if "wgt" not in key[0]:
                    continue
                output.loc[
                    ((abs(output.e1_eta) < 1.442) & (abs(output.e2_eta) < 1.442)),
                    key[0],
                ] = (
                    output.loc[
                        ((abs(output.e1_eta) < 1.442) & (abs(output.e2_eta) < 1.442)),
                        key[0],
                    ]
                    * NNPDFWeight(
                        mass_bb, leadingPt_bb, "bb", "el", float(self.year), DY=True
                    )
                ).values
                output.loc[
                    ((abs(output.e1_eta) > 1.566) | (abs(output.e2_eta) > 1.566)),
                    key[0],
                ] = (
                    output.loc[
                        ((abs(output.e1_eta) > 1.566) | (abs(output.e2_eta) > 1.566)),
                        key[0],
                    ]
                    * NNPDFWeight(
                        mass_be, leadingPt_be, "be", "el", float(self.year), DY=True
                    )
                ).values
        if is_mc and "ttbar" in dataset and self.applyNNPDFWeight:
            mass_bb = output[output["r"] == "bb"].dielectron_mass_gen.to_numpy()
            mass_be = output[output["r"] == "be"].dielectron_mass_gen.to_numpy()
            leadingPt_bb = output[output["r"] == "bb"].e1_pt_gen.to_numpy()
            leadingPt_be = output[output["r"] == "be"].e1_pt_gen.to_numpy()
            for key in output.columns:
                if "wgt" not in key[0]:
                    continue
                output.loc[
                    ((abs(output.e1_eta) < 1.442) & (abs(output.e2_eta) < 1.442)),
                    key[0],
                ] = (
                    output.loc[
                        ((abs(output.e1_eta) < 1.442) & (abs(output.e2_eta) < 1.442)),
                        key[0],
                    ]
                    * NNPDFWeight(
                        mass_bb, leadingPt_bb, "bb", "el", float(self.year), DY=False
                    )
                ).values
                output.loc[
                    ((abs(output.e1_eta) > 1.566) | (abs(output.e2_eta) > 1.566)),
                    key[0],
                ] = (
                    output.loc[
                        ((abs(output.e1_eta) > 1.566) | (abs(output.e2_eta) > 1.566)),
                        key[0],
                    ]
                    * NNPDFWeight(
                        mass_be, leadingPt_be, "be", "el", float(self.year), DY=False
                    )
                ).values

        output = output.loc[output.event_selection, :]
        output = output.reindex(sorted(output.columns), axis=1)
        output = output[output.r.isin(self.regions)]
        output.columns = output.columns.droplevel("Variation")

        if self.timer:
            self.timer.add_checkpoint("Filled outputs")
            self.timer.summary()

        if self.apply_to_output is None:
            return output
        else:
            self.apply_to_output(output)
            return self.accumulator.identity()

    def jet_loop(
        self,
        variation,
        is_mc,
        df,
        dataset,
        mask,
        electrons,
        e1,
        e2,
        jets,
        jet_branches,
        weights,
        numevents,
        output,
    ):

        if not is_mc and variation != "nominal":
            return

        variables = pd.DataFrame(index=output.index)
        jet_branches_local = copy.copy(jet_branches)

        if is_mc:
            jet_branches_local += [
                "partonFlavour",
                "hadronFlavour",
                "pt_gen",
                "eta_gen",
                "phi_gen",
            ]
            jets["pt_gen"] = jets.matched_gen.pt
            jets["eta_gen"] = jets.matched_gen.eta
            jets["phi_gen"] = jets.matched_gen.phi

        # Find jets that have selected muons within dR<0.4 from them
        matched_ele_pt = jets.matched_electrons.pt
        matched_ele_id = jets.matched_electrons[self.parameters["electron_id"]]
        matched_ele_pass = (
            (matched_ele_pt > self.parameters["electron_pt_cut"]) &
            matched_ele_id
        )
        clean = ~(ak.to_pandas(matched_ele_pass).astype(float).fillna(0.0)
                  .groupby(level=[0, 1]).sum().astype(bool))

        if self.timer:
             self.timer.add_checkpoint("Clean jets from matched electrons")

        # --- conversion from awkward to pandas --- #
        jets = ak.to_pandas(jets)

        if jets.index.nlevels == 3:
            # sometimes there are duplicates?
            jets = jets.loc[pd.IndexSlice[:, :, 0], :]
            jets.index = jets.index.droplevel("subsubentry")

        # ------------------------------------------------------------#
        # Apply jetID
        # ------------------------------------------------------------#
        # Sort jets by pT and reset their numbering in an event
        # jets = jets.sort_values(["entry", "pt"], ascending=[True, False])
        jets.index = pd.MultiIndex.from_arrays(
            [jets.index.get_level_values(0), jets.groupby(level=0).cumcount()],
            names=["entry", "subentry"],
        )

        jets = jets.dropna()
        jets = jets.loc[:, ~jets.columns.duplicated()]
 
        if self.do_btag:
            if is_mc:
                btagSF(jets, self.year, correction="shape", is_UL=True)
                btagSF(jets, self.year, correction="wp", is_UL=True)

                variables["wgt_nominal"] = (
                    jets.loc[jets.pre_selection == 1, "btag_sf_wp"]
                    .groupby("entry")
                    .prod()
                )
                variables["wgt_nominal"] = variables["wgt_nominal"].fillna(1.0)
                variables["wgt_nominal"] = variables[
                    "wgt_nominal"
                ] * weights.get_weight("nominal")
                variables["wgt_btag_up"] = (
                    jets.loc[jets.pre_selection == 1, "btag_sf_wp_up"]
                    .groupby("entry")
                    .prod()
                )
                variables["wgt_btag_up"] = variables["wgt_btag_up"].fillna(1.0)
                variables["wgt_btag_up"] = variables[
                    "wgt_btag_up"
                ] * weights.get_weight("nominal")
                variables["wgt_btag_down"] = (
                    jets.loc[jets.pre_selection == 1, "btag_sf_wp_down"]
                    .groupby("entry")
                    .prod()
                )
                variables["wgt_btag_down"] = variables["wgt_btag_down"].fillna(1.0)
                variables["wgt_btag_down"] = variables[
                    "wgt_btag_down"
                ] * weights.get_weight("nominal")

            else:
                variables["wgt_nominal"] = 1.0
                variables["wgt_btag_up"] = 1.0
                variables["wgt_btag_down"] = 1.0
        else:
            if is_mc:
                variables["wgt_nominal"] = 1.0
                variables["wgt_nominal"] = variables[
                    "wgt_nominal"
                ] * weights.get_weight("nominal")

            else:
                variables["wgt_nominal"] = 1.0

        jets["selection"] = 0
        jets.loc[
            ((jets.pt > 30.0) & (abs(jets.eta) < 2.4) & (jets.jetId >= 2)),
            "selection",
        ] = 1
 
        njets = jets.loc[:, "selection"].groupby("entry").sum() 
        variables["njets"] = njets 

        jets["bselection"] = 0
        jets.loc[
            (
                (jets.pt > 30.0)
                & (abs(jets.eta) < 2.4)
                & (jets.btagDeepFlavB > parameters["UL_btag_medium"][self.year])
                & (jets.jetId >= 2)
            ),
            "bselection",
        ] = 1

        nbjets = jets.loc[:, "bselection"].groupby("entry").sum()
        variables["nbjets"] = nbjets

        bjets = jets.query("bselection==1")
        bjets = bjets.sort_values(["entry", "pt"], ascending=[True, False])
        bjet1 = bjets.groupby("entry").nth(0)
        bjet2 = bjets.groupby("entry").nth(1)
        bJets = [bjet1, bjet2]
        electrons = [e1, e2]
        fill_bjets(output, variables, bJets, electrons, is_mc=is_mc)

        jets = jets.sort_values(["entry", "pt"], ascending=[True, False])
        jet1 = jets.groupby("entry").nth(0)
        jet2 = jets.groupby("entry").nth(1)
        Jets = [jet1, jet2]
        fill_jets(output, variables, Jets,  is_mc=is_mc)
        if self.timer:
            self.timer.add_checkpoint("Filled jet variables")


        # --------------------------------------------------------------#
        # Fill outputs
        # --------------------------------------------------------------#

        # All variables are affected by jet pT because of jet selections:
        # a jet may or may not be selected depending on pT variation.

        for key, val in variables.items():
            output.loc[:, key] = val

        del df
        del electrons
        del jets
        del e1
        del e2

        return output

    def prepare_lookups(self):
        # Pile-up reweighting
        self.pu_lookups = pu_lookups(self.parameters)
        self.jec_factories, self.jec_factories_data = jec_factories(self.year)
        # --- Evaluator
        #self.extractor = extractor()

        # Z-pT reweigting (disabled)
        #zpt_filename = self.parameters["zpt_weights_file"]
        #self.extractor.add_weight_sets([f"* * {zpt_filename}"])
        #if "2016" in self.year:
        #    self.zpt_path = "zpt_weights/2016_value"
        #else:
        #    self.zpt_path = "zpt_weights/2017_value"

        # Calibration of event-by-event mass resolution
        #for mode in ["Data", "MC"]:
        #    label = f"res_calib_{mode}_{self.year}"
        #    path = self.parameters["res_calib_path"]
        #    file_path = f"{path}/{label}.root"
        #    self.extractor.add_weight_sets([f"{label} {label} {file_path}"])

        #self.extractor.finalize()
        #self.evaluator = self.extractor.make_evaluator()

        #self.evaluator[self.zpt_path]._axes = self.evaluator[self.zpt_path]._axes[0]
        return

    def postprocess(self, accumulator):
        return accumulator
