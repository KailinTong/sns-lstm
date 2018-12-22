"""Module that defines the SocialLSTM model."""
import tensorflow as tf
import trajectory_decoders


class SocialModel:
    """SocialModel defines the model of the Social LSTM paper."""

    def __init__(
        self,
        dataset,
        helper,
        position_estimate,
        loss_function,
        pooling_module=None,
        lstm_size=128,
        max_num_ped=100,
        prediction_time=8,
        trajectory_size=20,
        embedding_size=64,
        learning_rate=0.003,
        clip_norm=5,
        l2_norm=0.001,
        opt_momentum=0.2,
        opt_decay=0.95,
        lr_steps=3000,
        lr_decay=0.95,
    ):
        """Constructor of the SocialModel class.

        Args:
          dataset: A TrajectoriesDataset instance.
          helper: A coordinates helper function.
          position_estimate: A position_estimate function.
          loss_function: a loss funtcion.
          pooling_module: A pooling module function
          lstm_size: int. The number of units in the LSTM cell.
          max_num_ped: int. Maximum number of pedestrian in a single frame.
          trajectories_size: int. Length of the trajectory (obs_length +
            pred_len).
          embedding_size: int. Dimension of the output space of the embedding
            layers.
          learning_rate: float. Learning rate.
          dropout: float. Dropout probability.
          clip_norm int. clip norm

        """
        # Create the tensor for input_data of shape
        # [max_num_ped, trajectory_size, 2]
        self.input_data = dataset.tensors[0]
        # Create the tensor for the ped mask of shape [max_num_ped, max_num_ped,
        # trajectory_size]
        self.peds_mask = dataset.tensors[1]
        # Create the tensor for num_peds_frame
        self.num_peds_frame = dataset.tensors[2]
        # Create the tensor for all pedestrians of shape
        # [max_num_ped, trajectory_size,2]
        self.all_peds = dataset.tensors[3]
        # Create the for the ped mask of shape
        # [max_num_ped, max_num_ped, trajectory_size]
        self.all_peds_mask = dataset.tensors[4]

        # Store the parameters
        # In training phase the list contains the values to minimize. In
        # sampling phase it has the coordinates predicted
        self.new_coordinates = []
        # The predicted coordinates processed by the linear layer in sampling
        # phase
        new_coordinates_processed = None
        # Output (or hidden states) of the LSTMs
        cell_output = tf.zeros([max_num_ped, lstm_size], tf.float32)

        # Output size
        output_size = 5

        # Counter for the adaptive learning rate. Counts the number of batch
        # processed.
        global_step = tf.Variable(0, trainable=False)

        # Define the LSTM with dimension lstm_size
        with tf.variable_scope("LSTM"):
            self.cell = tf.nn.rnn_cell.LSTMCell(lstm_size, name="Cell")

            # Define the states of the LSTMs. zero_state returns a tensor of
            # shape [max_num_ped, state_size]
            with tf.name_scope("States"):
                self.cell_states = self.cell.zero_state(max_num_ped, tf.float32)

        # Define the layer with ReLu used for processing the coordinates
        self.coordinates_layer = tf.layers.Dense(
            embedding_size,
            activation=tf.nn.relu,
            kernel_initializer=tf.contrib.layers.xavier_initializer(),
            name="Coordinates/Layer",
        )

        # Define the layer with ReLu used as output_layer for the decoder
        self.output_layer = tf.layers.Dense(
            output_size,
            kernel_initializer=tf.contrib.layers.xavier_initializer(),
            name="Position_estimation/Layer",
        )

        # Define the SocialTrajectoryDecoder.
        decoder = trajectory_decoders.SocialDecoder(
            self.cell,
            max_num_ped,
            helper,
            pooling_module=pooling_module,
            output_layer=self.output_layer,
        )

        # Decode the coordinates
        for frame in range(trajectory_size - 1):
            # Processing the coordinates
            self.coordinates_preprocessed = self.coordinates_layer(
                self.input_data[:, frame]
            )
            all_peds_preprocessed = self.coordinates_layer(self.all_peds[:, frame])

            # Initialize the decoder passing the real coordinates, the
            # coordinates that the model has predicted and the states of the
            # LSTMs. Which coordinates the model will use will be decided by the
            # helper function
            decoder.initialize(
                frame,
                self.coordinates_preprocessed,
                new_coordinates_processed,
                self.cell_states,
                all_peds_preprocessed,
                hidden_states=cell_output,
                peds_mask=self.peds_mask[:, :, frame],
                all_peds_mask=self.all_peds_mask[:, :, frame],
            )
            # compute_pass returns a tuple of two tensors. cell_output are the
            # output of the self.cell with shape [max_num_ped , output_size] and
            # cell_states are the states with shape
            # [max_num_ped, LSTMStateTuple()]
            cell_output, self.cell_states, layered_output = decoder.step()

            # Compute the new coordinates
            new_coordinates, new_coordinates_processed = position_estimate(
                layered_output,
                self.input_data[:, frame + 1],
                output_size,
                self.coordinates_layer,
            )

            # Append new_coordinates
            self.new_coordinates.append(new_coordinates)

        # self.new_coordinates has shape [trajectory_size - 1, max_num_ped]
        self.new_coordinates = tf.stack(self.new_coordinates)

        with tf.variable_scope("Calculate_loss"):
            self.loss = loss_function(
                self.new_coordinates[-prediction_time:, : self.num_peds_frame]
            )
            self.loss = tf.div(self.loss, tf.cast(self.num_peds_frame, tf.float32))

        # Add weights regularization
        tvars = tf.trainable_variables()
        l2_loss = (
            tf.add_n([tf.nn.l2_loss(v) for v in tvars if "bias" not in v.name])
            * l2_norm
        )
        self.loss = self.loss + l2_loss

        # Step epoch learning rate decay
        learning_rate = tf.train.exponential_decay(
            learning_rate, global_step, lr_steps, lr_decay
        )

        # Define the RMSProp optimizer
        optimizer = tf.train.RMSPropOptimizer(
            learning_rate, decay=opt_decay, momentum=opt_momentum, centered=True
        )
        # Global norm clipping
        gradients, variables = zip(*optimizer.compute_gradients(self.loss))
        clipped, _ = tf.clip_by_global_norm(gradients, clip_norm)
        self.trainOp = optimizer.apply_gradients(
            zip(clipped, variables), global_step=global_step
        )
