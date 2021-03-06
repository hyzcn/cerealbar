from __future__ import annotations

import numpy as np
import torch
from torch import nn

from agent import util
from agent.data import aggregated_instruction_example
from agent.data import instruction_example
from agent.environment import position
from agent.evaluation import evaluation_logger
from agent.learning import auxiliary
from agent.learning import plan_losses
from agent.learning import util as learning_util
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Dict, List, Optional, Tuple
    from agent.data import partial_observation
    from agent.model.model_wrappers import model_wrapper


def normalize_trajectory_distribution(map_distribution: torch.Tensor) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
    batch_size = map_distribution.size(0)
    flat_distribution: torch.Tensor = map_distribution.view(batch_size, -1)

    return nn.Softmax(dim=1)(flat_distribution).view(map_distribution.size())


def add_trajectory_metrics(metric_results: Dict[str, Any],
                           example: instruction_example.InstructionExample,
                           predicted_map_distribution: torch.Tensor,
                           full_observability: bool,
                           weight_by_time: bool,
                           observation: partial_observation.PartialObservation) -> None:
    """
    Computes and adds metrics for trajectory cross-entropy.
    """
    metric_results[str(auxiliary.Auxiliary.TRAJECTORY) + ' xent'].append(
        plan_losses.compute_trajectory_loss(example,
                                            predicted_map_distribution,
                                            full_observability=full_observability,
                                            weight_by_time=weight_by_time,
                                            observation=None if full_observability else observation).item())


def get_hexes_above_threshold(map_probabilities: torch.Tensor,
                              valid_positions: List[position.Position] = None) -> List[position.Position]:
    """
    :param map_probabilities: Tensor the size of the map containing probabilities for each hex of reaching that hex.
    :param valid_positions: A list containing the valid positions to return.
    :return: List of positions in the map with over 0.5 probability.
    """
    predicted_positions: List[position.Position] = list()
    width, depth = map_probabilities.size()
    for x_pos in range(width):
        for y_pos in range(depth):
            if map_probabilities[x_pos][y_pos].item() > 0.5:
                pos: position.Position = position.Position(x_pos, y_pos)
                if valid_positions is None or pos in valid_positions:
                    predicted_positions.append(pos)
    return predicted_positions


def add_card_metrics(metric_results: Dict[str, Any],
                     predicted_positions: List[position.Position],
                     gold_positions: List[position.Position],
                     prefix: str) -> None:
    """ Computes and adds metrics for card predictions.
    :param metric_results: The dictionary to add the results to.
    :param predicted_positions: The predicted positions.
    :param gold_positions: The gold positions.
    :param prefix: The prefix to log with.
    """
    accuracy, precision, recall = learning_util.evaluate_set_precision_recall(predicted_positions, gold_positions)
    metric_results[prefix + ' accuracy'].append(accuracy)
    metric_results[prefix + ' precision'].append(precision)
    metric_results[prefix + ' recall'].append(recall)


def plan_metric_results(model: model_wrapper.ModelWrapper,
                        examples: Dict[str, instruction_example.InstructionExample],
                        logging_filename: str = '') -> Dict[str, float]:
    """ Evaluates a hex predictor model over a set of examples assuming the agent takes the correct path. """

    logger: evaluation_logger.EvaluationLogger = evaluation_logger.EvaluationLogger(logging_filename)

    full_observability: bool = model.get_arguments().get_state_rep_args().full_observability()
    maximum_observation_age: int = model.get_arguments().get_state_rep_args().get_observation_memory_size()
    evaluation_ids = instruction_example.get_example_action_index_pairs(
        examples, full_observability,
        model.get_arguments().get_state_rep_args().observability_refresh_rate())

    auxiliary_predictions_dict = dict()
    with util.get_progressbar('evaluating...', len(evaluation_ids)) as pbar:
        for i, (example_id, action_idx) in enumerate(evaluation_ids):
            pbar.update(i)
            auxiliaries = model.get_predictions(
                examples[example_id],
                examples[example_id].get_partial_observations()[action_idx] if action_idx >= 0 else None)
            auxiliary_predictions_dict[(example_id, action_idx)] = auxiliaries

    metric_results: Dict[str, Any] = dict()

    if auxiliary.Auxiliary.INTERMEDIATE_GOALS in model.get_auxiliaries():
        metric_results[str(auxiliary.Auxiliary.INTERMEDIATE_GOALS) + ' precision'] = list()
        metric_results[str(auxiliary.Auxiliary.INTERMEDIATE_GOALS) + ' recall'] = list()
        metric_results[str(auxiliary.Auxiliary.INTERMEDIATE_GOALS) + ' accuracy'] = list()
    if auxiliary.Auxiliary.FINAL_GOALS in model.get_auxiliaries():
        metric_results[str(auxiliary.Auxiliary.FINAL_GOALS) + ' precision'] = list()
        metric_results[str(auxiliary.Auxiliary.FINAL_GOALS) + ' recall'] = list()
        metric_results[str(auxiliary.Auxiliary.FINAL_GOALS) + ' accuracy'] = list()

    if auxiliary.Auxiliary.TRAJECTORY in model.get_auxiliaries():
        metric_results[str(auxiliary.Auxiliary.TRAJECTORY) + ' xent'] = list()

    if auxiliary.Auxiliary.OBSTACLES in model.get_auxiliaries():
        metric_results[str(auxiliary.Auxiliary.OBSTACLES) + ' accuracy'] = list()
        metric_results[str(auxiliary.Auxiliary.OBSTACLES) + ' recall'] = list()
        metric_results[str(auxiliary.Auxiliary.OBSTACLES) + ' precision'] = list()

    if auxiliary.Auxiliary.AVOID_LOCS in model.get_auxiliaries():
        metric_results[str(auxiliary.Auxiliary.AVOID_LOCS) + ' accuracy'] = list()
        metric_results[str(auxiliary.Auxiliary.AVOID_LOCS) + ' recall'] = list()
        metric_results[str(auxiliary.Auxiliary.AVOID_LOCS) + ' precision'] = list()

    if auxiliary.Auxiliary.IMPLICIT in model.get_auxiliaries():
        for i in range(model.get_arguments().get_state_encoder_args().get_encoder_depth()):
            metric_results[str(auxiliary.Auxiliary.IMPLICIT) + ' accuracy layer ' + str(i)] = list()

    for example_id, action_index in evaluation_ids:
        example = examples[example_id]

        logger.log('***** Example #' + example_id + ' *****')
        logger.log('Instruction: ' + ' '.join(example.get_instruction()))
        logger.log('Action index: ' + str(action_index))

        visible_cards = example.get_initial_cards()
        if not full_observability:
            visible_cards = example.get_partial_observations()[action_index].get_card_beliefs()

        all_card_positions = sorted(list(set([visible_card.get_position() for visible_card in visible_cards])))

        auxiliary_predictions = auxiliary_predictions_dict[(example.get_id(), action_index)]

        # TODO: Should this be computed differently? E.g. depending on whether the encoded information is updated
        gold_position_set = sorted(list(set([card.get_position() for card in example.get_touched_cards()])))

        if not full_observability:
            # Limit the gold set to be only the cards that are visible on the board
            gold_position_set = sorted(list(set(gold_position_set) & set(all_card_positions)))

        if logger.active():
            logger.log('Goal cards:')
            for goal_card in example.get_touched_cards():
                visibility: str = 'Not visible!' if goal_card.get_position() not in all_card_positions else ''
                logger.log('\t' + str(goal_card) + '\t' + visibility)

        if auxiliary.Auxiliary.FINAL_GOALS in model.get_auxiliaries():
            predicted_positions = sorted(list(set(get_hexes_above_threshold(
                auxiliary_predictions[auxiliary.Auxiliary.FINAL_GOALS][0],
                all_card_positions))))

            if logger.active():
                logger.log('Final card predictions:')
                for visible_card in visible_cards:
                    if visible_card.get_position() in predicted_positions:
                        logger.log('\t' + str(visible_card))

            add_card_metrics(metric_results,
                             predicted_positions,
                             gold_position_set,
                             str(auxiliary.Auxiliary.FINAL_GOALS))

        if auxiliary.Auxiliary.INTERMEDIATE_GOALS in model.get_auxiliaries():
            add_card_metrics(metric_results,
                             sorted(list(set(get_hexes_above_threshold(
                                 auxiliary_predictions[auxiliary.Auxiliary.INTERMEDIATE_GOALS],
                                 all_card_positions)))),
                             gold_position_set,
                             str(auxiliary.Auxiliary.INTERMEDIATE_GOALS))

        if auxiliary.Auxiliary.AVOID_LOCS in model.get_auxiliaries():
            # These are all the positions where cards are believed to be except (1) the cards that should be touched
            # and (2) the card it starts on
            touched_plus_initial = [card.get_position() for card in example.get_touched_cards(
                include_start_position=True)]
            gold_positions: List[position.Position] = sorted(list(set(
                [card_position for card_position in all_card_positions if card_position not in touched_plus_initial])))

            pred_positions: List[position.Position] = \
                sorted(list(set(get_hexes_above_threshold(auxiliary_predictions[auxiliary.Auxiliary.AVOID_LOCS][0],
                                                          all_card_positions))))
            acc, prec, recall = learning_util.evaluate_set_precision_recall(pred_positions, gold_positions)
            metric_results[str(auxiliary.Auxiliary.AVOID_LOCS) + ' accuracy'].append(acc)
            metric_results[str(auxiliary.Auxiliary.AVOID_LOCS) + ' precision'].append(prec)
            metric_results[str(auxiliary.Auxiliary.AVOID_LOCS) + ' recall'].append(recall)

        if auxiliary.Auxiliary.TRAJECTORY in model.get_auxiliaries():
            add_trajectory_metrics(metric_results,
                                   example,
                                   auxiliary_predictions[auxiliary.Auxiliary.TRAJECTORY],
                                   full_observability,
                                   model.get_arguments().get_decoder_args().weight_trajectory_by_time(),
                                   example.get_partial_observations()[action_index])

        if auxiliary.Auxiliary.OBSTACLES in model.get_auxiliaries():
            gold_positions: List[position.Position] = sorted(list(set(example.get_obstacle_positions())))
            pred_positions: List[position.Position] = \
                sorted(list(set(get_hexes_above_threshold(auxiliary_predictions[auxiliary.Auxiliary.OBSTACLES][0]))))

            if not full_observability:
                # Limit the gold and predicted positions only to the visible positions.
                gold_positions = sorted(list(set(gold_positions)
                                             & example.get_partial_observations()[
                                                 action_index].lifetime_observed_positions(maximum_observation_age)))
                pred_positions = sorted(list(set(pred_positions)
                                             & example.get_partial_observations()[
                                                 action_index].lifetime_observed_positions(maximum_observation_age)))

            acc, prec, recall = learning_util.evaluate_set_precision_recall(pred_positions, gold_positions)
            metric_results[str(auxiliary.Auxiliary.OBSTACLES) + ' accuracy'].append(acc)
            metric_results[str(auxiliary.Auxiliary.OBSTACLES) + ' precision'].append(prec)
            metric_results[str(auxiliary.Auxiliary.OBSTACLES) + ' recall'].append(recall)

        if auxiliary.Auxiliary.IMPLICIT in model.get_auxiliaries():
            implicit_preds = auxiliary_predictions[auxiliary.Auxiliary.IMPLICIT].tolist()

            for i, pred in enumerate(implicit_preds):
                label = isinstance(example, aggregated_instruction_example.AggregatedInstructionExample) and \
                        example.implicit()
                pred_label = pred > 0.5

                if label == pred_label:
                    metric_results[str(auxiliary.Auxiliary.IMPLICIT) + ' accuracy layer ' + str(i)].append(1.)
                else:
                    metric_results[str(auxiliary.Auxiliary.IMPLICIT) + ' accuracy layer ' + str(i)].append(0.)
        logger.log('\n')

    # Compute the means
    final_results: Dict[str, float] = dict()
    for key, value in metric_results.items():
        final_results[key] = float(np.mean(np.array(value)))

    logger.close()

    return final_results
