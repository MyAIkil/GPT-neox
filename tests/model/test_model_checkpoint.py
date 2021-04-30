import os
import shutil
import sys
import unittest
from unittest.mock import patch

if __name__ == "__main__":
    sys.path.append(os.path.abspath(''))

from megatron.neox_arguments import NeoXArgs

from megatron.global_vars import get_args, reset_global_variables

from megatron import initialize_megatron
from megatron.text_generation_utils import get_batch, forward_model
from megatron.training import setup_model_and_optimizer

from megatron.checkpointing import load_checkpoint
from megatron.checkpointing import save_checkpoint
from pretrain_gpt2 import model_provider
from megatron.utils import pipe_to_normal
from deepspeed import PipelineEngine

from tests.common import get_root_directory, get_configs_with_path
import torch

import logging

class TestModelCheckpoint(unittest.TestCase):

    def run_checkpoint_test(self, config_yml):
        reset_global_variables()

        # intitially load config from files as would be the case in deepy.py
        yaml_list = get_configs_with_path(["local_setup.yml"])
        yaml_list.append(f"{get_root_directory()}/tests/model/test_configs/{config_yml}")
        logging.info(
            self.__class__.__name__ + ".run_checkpoint_test() " + f"Running on: {yaml_list}")

        args_loaded = NeoXArgs.from_ymls(yaml_list)
        args_loaded.update_value("user_script", str(get_root_directory() / "pretrain_gpt2.py"))
        args_loaded.update_value("pipe_parallel_size", 1) # overwrite pipeline parameter, config in small.yml may have changed!
        args_loaded.update_value("num_unique_layers", 4)
        args_loaded.update_value("use_cpu_initialization", True)
        args_loaded.update_value("save", "test_checkpoint")
        args_loaded.update_value("load", "test_checkpoint")

        deepspeed_main_args = args_loaded.get_deepspeed_main_args()

        # patch sys.argv so that args can be access by set_global_variables within initialize_megatron
        with patch('sys.argv', deepspeed_main_args):
            initialize_megatron()
        logging.info(
            self.__class__.__name__ + ".run_checkpoint_test() " + "initializing megatron")

        # load args from global variables
        args = get_args()

        # remove any existing checkpoints if they exist
        path = os.path.join(get_root_directory(), args.load)
        shutil.rmtree(path)

        # Initialize new model model
        model, optimizer, lr_scheduler = setup_model_and_optimizer(lambda: model_provider(use_wandb=False))
        model.eval()


        # save model checkpoint
        save_checkpoint(42, model, optimizer, lr_scheduler)
        
        logging.info(
            self.__class__.__name__ + ".run_checkpoint_test() " + "saving checkpoint")
        
        context_tokens_tensor = torch.cuda.LongTensor([[1,2,3,4,5],[1,2,3,4,5],[6,7,8,9,10],[1,2,3,4,100]])

        tokens, attention_mask, position_ids = get_batch(context_tokens_tensor)
        output = forward_model(model, (tokens, position_ids, attention_mask))

        logging.info(
            self.__class__.__name__ + ".run_checkpoint_test() " + "running tests")

        # assert outputs are the right shape
        self.assertEqual(output.size(0), args.batch_size)
        self.assertEqual(output.size(1), context_tokens_tensor.size(1))
        # assert model output is a tensor
        self.assertTrue(isinstance(output, torch.Tensor))

        # assert correct behaviour
        self.assertTrue(torch.isclose(output[0], output[1]).all().item())
        self.assertFalse(torch.isclose(output[1], output[2]).all().item())
        self.assertTrue(torch.isclose(output[1, 3], output[3, 3]).all().item())
        
        # reload model from checkpoint
        reloaded_model, optimizer, lr_scheduler = setup_model_and_optimizer(lambda: model_provider(use_wandb=False))

        logging.info(
            self.__class__.__name__ + ".run_checkpoint_test() " + "reloading checkpoint")

        iteration = load_checkpoint(reloaded_model, optimizer, lr_scheduler)
        reloaded_model.eval()

        #ensure same checkpoint is loaded
        self.assertEqual(iteration, 42)

        reloaded_output = forward_model(model, (tokens, position_ids, attention_mask))

        #check re-loaded model returns the same results
        self.assertTrue(torch.isclose(output, reloaded_output).all().item())

        #check all weight groups are the same
        for idx, ((n1, p1), (n2, p2)) in enumerate(zip(list(model.module.named_parameters()), list(reloaded_model.module.named_parameters()))):
            self.assertTrue(n1 == n2)
            params_equal = (p1 == p2).all().item()
            self.assertTrue(params_equal)
            if not params_equal:
                logging.warning(
                    self.__class__.__name__ + ".run_checkpoint_test() " + f"layer {idx} {n1} has same different after loading of checkpoint")

        #clear up checkpoint folder
        logging.info(
            self.__class__.__name__ + ".run_checkpoint_test() " + "cleaning checkpoint")

        shutil.rmtree(path)

    def test_model_small(self):
        self.run_checkpoint_test("small.yml")

    def test_model_medium(self):
        self.run_checkpoint_test("medium.yml")

if __name__ == "__main__":
    suite = unittest.TestSuite()

    #Run all required tests
    suite.addTest(TestModelCheckpoint("test_model_small"))
    suite.addTest(TestModelCheckpoint("test_model_medium"))

    unittest.TextTestRunner(failfast=False).run(suite)